#!/bin/bash
#SBATCH -J c2c_probe
#SBATCH -p gh-dev
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -t 00:15:00
#SBATCH -A ASC26114
#SBATCH -o c2c_%j.out
module load cuda 2>&1
set -x
hostname; lscpu | head -20; numactl -H; nvidia-smi; nvidia-smi -q | grep -iA3 -E "addressing|c2c" ; nvidia-smi c2c -s 2>&1 | head
set +x
nvcc -O3 -arch=sm_90 -std=c++17 -o c2c_bench c2c_bench.cu || exit 1
./c2c_bench
