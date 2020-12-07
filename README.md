# Pybullet Grasp Simulation 

Based on [Robot Graspit! Project][cr_grasper] ([MIT License][cr_grasper-lic]). See [SOURCE.md](SOURCE.md) for original documentation.

### Installation

```bash
conda create -n $(basename $(pwd)) python=3 && conda activate $(basename $(pwd))
pip3 install pybullet \
              astropy \
              transforms3d \
              pyquaternion \
              scipy

```

### Run

```bash
python cr_grasper/grasper.py
```


---

[cr_grasper]: https://github.com/carcamdou/cr_grasper
[cr_grasper-lic]: https://github.com/carcamdou/cr_grasper/blob/master/setup.py#L14