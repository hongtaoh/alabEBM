import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from numba import njit, prange, float64, int32
from typing import Dict, List, Tuple, Union, Optional, Any
import numba
from alabebm.utils.kmeans import get_two_clusters_with_kmeans

# Constants
EPSILON = 1e-10
SQRT_2PI = 0.3989422804014327  # 1/sqrt(2*pi)

@njit(float64(float64, float64, float64), fastmath=True, cache=True)
def gaussian_kernel(x: float, xi: float, bandwidth: float) -> float:
    """
    Gaussian kernel function optimized with Numba.
    
    Args:
        x: Point at which to evaluate the kernel
        xi: Sample point
        bandwidth: Kernel bandwidth parameter
        
    Returns:
        Kernel value at point x
    """
    u = (x - xi) / bandwidth
    return SQRT_2PI * np.exp(-0.5 * u * u) / bandwidth

@njit(float64(float64, float64[:], float64[:], float64), parallel=True, cache=True)
def weighted_kde_evaluate(x: float, samples: np.ndarray, weights: np.ndarray, bandwidth: float) -> float:
    """
    Evaluate KDE at point x using weighted samples with parallel processing.
    
    Args:
        x: Point at which to evaluate the KDE
        samples: Array of sample points
        weights: Array of weights for each sample
        bandwidth: Kernel bandwidth parameter
        
    Returns:
        Weighted KDE value at point x
    """
    result = 0.0
    for i in prange(len(samples)):
        result += weights[i] * gaussian_kernel(x, samples[i], bandwidth)
    return result

@njit(parallel=True, cache=True)
def batch_weighted_kde_evaluate(points: np.ndarray, samples: np.ndarray, 
                               weights: np.ndarray, bandwidth: float) -> np.ndarray:
    """
    Vectorized KDE evaluation for multiple points.
    
    Args:
        points: Array of points at which to evaluate the KDE
        samples: Array of sample points
        weights: Array of weights for each sample
        bandwidth: Kernel bandwidth parameter
        
    Returns:
        Array of KDE values at each point
    """
    results = np.zeros(len(points), dtype=np.float64)
    for i in prange(len(points)):
        results[i] = weighted_kde_evaluate(points[i], samples, weights, bandwidth)
    return results

class FastKDE:
    """
    Fast Kernel Density Estimation implementation with caching and optimizations.
    
    Attributes:
        data: Sample points
        weights: Weight for each sample point
        bandwidth: Kernel bandwidth parameter
        tree: KD-tree for fast neighbor lookups
    """
    
    def __init__(self, data: np.ndarray, weights: Optional[np.ndarray] = None, 
                 bandwidth: Union[str, float] = 'silverman'):
        """
        Initialize the FastKDE estimator.
        
        Args:
            data: Sample points
            weights: Weight for each sample point (defaults to uniform)
            bandwidth: Either 'silverman' for automatic bandwidth selection,
                      or a float specifying the bandwidth directly
        """
        self.data = np.asarray(data, dtype=np.float64).flatten()
        
        # Handle weights
        if weights is None:
            self.weights = np.ones_like(self.data, dtype=np.float64)
        else:
            self.weights = np.asarray(weights, dtype=np.float64).flatten()
            
        # Normalize weights
        weight_sum = np.sum(self.weights)
        if weight_sum > 0:
            self.weights /= weight_sum
        
        # Set bandwidth
        if bandwidth == 'silverman':
            n = len(self.data)
            sigma = max(np.std(self.data), 1e-6)  # Prevent zero bandwidth
            self.bandwidth = sigma * (4 / (3 * n)) ** (1/5)
        else:
            self.bandwidth = max(float(bandwidth), 1e-6)  # Prevent zero bandwidth
            
        # Create KD-tree for nearest neighbor lookup
        self.tree = cKDTree(self.data[:, np.newaxis])
        
        # Cache for repeated evaluations
        self._cache = {}
    
    def evaluate(self, points: np.ndarray) -> np.ndarray:
        """
        Evaluate the KDE at the specified points.
        
        Args:
            points: Points at which to evaluate the KDE
            
        Returns:
            KDE values at each point
        """
        points = np.asarray(points, dtype=np.float64).flatten()
        results = np.zeros(len(points), dtype=np.float64)
        
        # For small number of points, process individually
        if len(points) <= 10:
            for i, x in enumerate(points):
                # Check cache first
                if x in self._cache:
                    results[i] = self._cache[x]
                    continue
                    
                # Query KD-tree for points within bandwidth neighborhood
                indices = self.tree.query_ball_point(x, 3 * self.bandwidth)
                
                if indices:
                    # Only compute with relevant neighbors
                    result = weighted_kde_evaluate(
                        x, self.data[indices], self.weights[indices], self.bandwidth
                    )
                    results[i] = result
                    
                    # Cache result
                    if len(self._cache) < 1000:  # Limit cache size
                        self._cache[x] = result
        else:
            # For larger point sets, use vectorized implementation
            # Split into chunks to maintain memory efficiency
            chunk_size = 1000
            for i in range(0, len(points), chunk_size):
                chunk = points[i:i+chunk_size]
                # For each point, find relevant neighbors
                for j, x in enumerate(chunk):
                    indices = self.tree.query_ball_point(x, 3 * self.bandwidth)
                    if indices:
                        results[i+j] = weighted_kde_evaluate(
                            x, self.data[indices], self.weights[indices], self.bandwidth
                        )
        
        return results
    
    def logpdf(self, points: np.ndarray) -> np.ndarray:
        """
        Compute log probability density function at the specified points.
        
        Args:
            points: Points at which to evaluate the log PDF
            
        Returns:
            Log PDF values at each point
        """
        return np.log(np.maximum(self.evaluate(points), EPSILON))

    def __eq__(self, other: Any) -> bool:
        """Check if two KDEs are effectively equivalent"""
        if not isinstance(other, FastKDE):
            return False
        
        return (np.array_equal(self.data, other.data) and 
                np.array_equal(self.weights, other.weights) and
                self.bandwidth == other.bandwidth)



@njit(cache=True)
def _compute_kde_logpdf(
    measurements: np.ndarray, 
    stages: np.ndarray, 
    bio_indices: np.ndarray,
    kde_data: np.ndarray, 
    kde_weights: np.ndarray, 
    bandwidths: np.ndarray
) -> float:
    """
    Compute KDE log PDF efficiently using Numba.
    
    Args:
        measurements: Biomarker measurements
        stages: Stage indicators (0/1)
        bio_indices: Biomarker indices
        kde_data: KDE sample points for each biomarker
        kde_weights: KDE weights for each biomarker
        bandwidths: Bandwidths for each biomarker
        
    Returns:
        Total log PDF value
    """
    total = 0.0
    
    for i in range(len(measurements)):
        idx = bio_indices[i]
        x = measurements[i]
        stage = stages[i]
        
        # Calculate position in pre-allocated arrays
        data_row = idx*2 + (0 if stage else 1)
        weights_row = idx*2 + (0 if stage else 1)
        
        data = kde_data[data_row]
        weights = kde_weights[weights_row]
        bw = bandwidths[idx]
        
        pdf = 0.0
        for j in range(len(data)):
            if data[j] == 0:  # Padding value check
                break
            pdf += weights[j] * gaussian_kernel(x, data[j], bw)
        
        # Handle numerical stability
        if pdf < EPSILON:
            total += np.log(EPSILON)
        else:
            total += np.log(pdf)
    
    return total


def compute_ln_likelihood_kde_fast(
    measurements: np.ndarray, 
    S_n: int, 
    biomarkers: np.ndarray, 
    k_j: np.ndarray, 
    kde_dict: Dict[str, Dict[str, Union[FastKDE, np.ndarray]]]
) -> float:
    """
    Optimized KDE likelihood computation.
    
    Args:
        measurements: Biomarker measurements
        S_n: Stage threshold
        biomarkers: Biomarker identifiers
        k_j: Stage values
        kde_dict: Dictionary of KDE objects for each biomarker
        
    Returns:
        Log likelihood value
    """
    # Convert to stage indicators
    stages = (k_j >= S_n).astype(np.int32)
    
    # Convert to arrays for Numba
    unique_bios = np.unique(biomarkers)
    bio_to_idx = {b: i for i, b in enumerate(unique_bios)}
    bio_indices = np.array([bio_to_idx[b] for b in biomarkers], dtype=np.int32)
    
    # Calculate maximum data size needed for arrays
    max_data_size = max(len(kde_dict[b]['theta_kde'].data) for b in unique_bios)
    max_data_size = max(max_data_size, max(len(kde_dict[b]['phi_kde'].data) for b in unique_bios))
    
    # Pre-allocate arrays with padding
    kde_data = np.zeros((len(unique_bios) * 2, max_data_size), dtype=np.float64)
    kde_weights = np.zeros((len(unique_bios) * 2, max_data_size), dtype=np.float64)
    bandwidths = np.zeros(len(unique_bios), dtype=np.float64)
    
    # Fill arrays with data
    for i, b in enumerate(unique_bios):
        theta_kde = kde_dict[b]['theta_kde']
        phi_kde = kde_dict[b]['phi_kde']
        
        # Fill theta data and weights
        data_size = len(theta_kde.data)
        kde_data[i*2, :data_size] = theta_kde.data
        kde_weights[i*2, :data_size] = theta_kde.weights
        
        # Fill phi data and weights
        data_size = len(phi_kde.data)
        kde_data[i*2+1, :data_size] = phi_kde.data
        kde_weights[i*2+1, :data_size] = phi_kde.weights
        
        # Store bandwidth
        bandwidths[i] = theta_kde.bandwidth
    
    # Compute log likelihood
    return _compute_kde_logpdf(
        measurements.astype(np.float64),
        stages,
        bio_indices,
        kde_data,
        kde_weights,
        bandwidths
    )


def get_initial_kde_estimates(
    data: pd.DataFrame
) -> Dict[str, Dict[str, Union[FastKDE, np.ndarray]]]:
    """
    Obtain initial KDE estimates for each biomarker.

    Args:
        data: DataFrame containing participant data with columns:
             - biomarker: Biomarker identifier
             - measurement: Biomarker measurement value

    Returns:
        Dictionary mapping biomarkers to their KDE parameters:
        {
            "biomarker1": {
                "theta_kde": FastKDE,
                "theta_weights": np.ndarray,
                "phi_kde": FastKDE,
                "phi_weights": np.ndarray,
            },
            ...
        }
    """
    estimates = {}
    biomarkers = data['biomarker'].unique()
    data_size = len(data.participant.unique())
    
    for biomarker in biomarkers:
        biomarker_df = data[data['biomarker'] == biomarker].reset_index(drop=True)
        
        # Skip biomarkers with too few measurements
        if len(biomarker_df) < 5:
            print(f"Warning: Skipping biomarker {biomarker} with only {len(biomarker_df)} measurements")
            continue
            
        # Get initial clusters
        theta_measurements, phi_measurements = get_two_clusters_with_kmeans(biomarker_df)
        
        # Create KDEs with uniform weights
        estimates[biomarker] = {
            'theta_kde': FastKDE(data=theta_measurements),
            'theta_weights': np.ones(data_size)/data_size,
            'phi_kde': FastKDE(data=phi_measurements),
            'phi_weights': np.ones(data_size)/data_size
        }
    
    return estimates

def update_kde_for_biomarker_em(
    biomarker: str,
    participants: np.ndarray,
    measurements: np.ndarray,
    diseased: np.ndarray,
    stage_post: Dict[int, np.ndarray],
    theta_phi_current: Dict[str, Union[FastKDE, np.ndarray]],
    disease_stages: np.ndarray,
    curr_order: int,
    weight_change_threshold: float = 0.01
) -> Tuple[FastKDE, np.ndarray, FastKDE, np.ndarray]:
    """
    Update KDE estimates for a biomarker using Expectation-Maximization.
    
    Args:
        biomarker: str
        participants: Participant IDs
        measurements: Biomarker measurements
        diseased: Boolean array indicating disease status
        stage_post: Dictionary mapping participant ID to posterior stage probabilities
        theta_phi_current: Current KDE estimates
        disease_stages: Array of disease stages
        curr_order: Current biomarker order
        weight_change_threshold: Threshold for determining when to update KDEs
        
    Returns:
        Tuple of (theta_kde, theta_weights, phi_kde, phi_weights)
    """
    measurements = np.array(measurements, dtype=np.float64)
    theta_weights = np.zeros_like(measurements, dtype=np.float64)
    phi_weights = np.zeros_like(measurements, dtype=np.float64)

    # Update weights based on current posterior estimates
    for i, (p, d) in enumerate(zip(participants, diseased)):
        if not d:
            # For non-diseased participants, all weight goes to phi
            phi_weights[i] = 1.0
        else:
            # For diseased participants, distribute weights based on stage
            probs = stage_post[p]
            theta_weights[i] = np.sum(probs[disease_stages >= curr_order])
            phi_weights[i] = np.sum(probs[disease_stages < curr_order])

    # Normalize weights
    if np.sum(theta_weights) > 0:
        theta_weights /= np.sum(theta_weights)
    else:
        # Handle edge case with no theta weights
        theta_weights = np.ones_like(theta_weights) / len(theta_weights)
        
    if np.sum(phi_weights) > 0:
        phi_weights /= np.sum(phi_weights)
    else:
        # Handle edge case with no phi weights
        phi_weights = np.ones_like(phi_weights) / len(phi_weights)

    # Theta KDE decision - only update if weights changed significantly
    if np.mean(np.abs(theta_weights - theta_phi_current[biomarker]['theta_weights'])) < weight_change_threshold:
        theta_kde = theta_phi_current[biomarker]['theta_kde']  # Reuse existing KDE
        theta_weights = theta_phi_current[biomarker]['theta_weights']  # Keep existing weights
    else:
        # Create new KDE with updated weights
        theta_kde = FastKDE(data=measurements, weights=theta_weights)
    
    # Phi KDE decision - only update if weights changed significantly
    if np.mean(np.abs(phi_weights - theta_phi_current[biomarker]['phi_weights'])) < weight_change_threshold:
        phi_kde = theta_phi_current[biomarker]['phi_kde']  # Reuse existing KDE
        phi_weights = theta_phi_current[biomarker]['phi_weights']  # Keep existing weights
    else:
        # Create new KDE with updated weights
        phi_kde = FastKDE(data=measurements, weights=phi_weights)

    return theta_kde, theta_weights, phi_kde, phi_weights