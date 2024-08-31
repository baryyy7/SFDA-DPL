#! /bin/sh

#SBATCH --job-name=dpl
#SBATCH --output=/users/scratch/baryaacovi-2024-06-01/projects/SFDA-DPL/slurm/dpl_source_d1_%n_%j_%t.out # tedirect stdout
#SBATCH --error=/users/scratch/baryaacovi-2024-06-01/projects/SFDA-DPL/slurm/dpl_source_d1_%n_%j_%t.err # redirect stderr
#SBATCH --partition=studentbatch # (see resources section)
#SBATCH --time=2880 # max time (minutes)
#SBATCH --signal=USR1@120 # how to end job when time’s up
#SBATCH --nodes=1 # number of machines
#SBATCH --ntasks=4 # number of processes
#SBATCH --mem=8100 # CPU memory (MB)
#SBATCH --cpus-per-task=2 # CPU cores per process
#SBATCH --gpus=1 # GPUs in total

#eval
#0707
/home/yandex/DL20222023a/baryaacovi/anaconda3/envs/teach/bin/python3 train_source.py 

