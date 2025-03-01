from alabEBM import run_ebm
from alabEBM.data import get_sample_data_path, get_biomarker_order_path
import os
import json 

cwd = os.getcwd()
print("Current Working Directory:", cwd)
data_dir = f"{cwd}/alabEBM/tests/my_data"
data_files = os.listdir(data_dir) 

# Get path to biomarker_order
biomarker_order_json = get_biomarker_order_path()

with open(biomarker_order_json, 'r') as file:
    biomarker_order = json.load(file)

for algorithm in ['hard_kmeans', 'soft_kmeans', 'conjugate_priors']:
    for data_file in data_files:
        results = run_ebm(
            data_file= f"{data_dir}/{data_file}",
            # data_file=get_sample_data_path('10|100_0.csv'),  # Use the path helper
            algorithm=algorithm,
            n_iter=100,
            n_shuffle=2,
            burn_in=50,
            thinning=10,
            correct_ordering=biomarker_order
        )