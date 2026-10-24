# Installs only the SDK with pip, for scripts that talk to the arm without ROS:
#   pip install -e .
# On the ROS PC you don't need this: colcon builds the SDK as the ROS package ros2/unitree_k1_sdk.
from setuptools import setup

setup(
    name="Unitree-K1-SDK",
    version="2024.11.01",
    description="Software driver for the Unitree K1 (Sagittarius) robotic arm",
    url="https://github.com/T-K-233/Unitree-K1-SDK",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3.10",
    ],
    package_dir={"": "ros2/unitree_k1_sdk"},
    packages=["unitree_k1_sdk"],
    python_requires=">=3.8",
    install_requires=[
        "pyserial",
        "numpy",
        "tqdm",
    ]
)
