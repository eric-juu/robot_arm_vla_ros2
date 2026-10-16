from setuptools import setup, find_packages

setup(
    name="Unitree-K1-SDK",
    version="2024.11.01",
    description="Software driver for the Unitree K1 (Sagittarius) robotic arm",
    url="https://github.com/T-K-233/Unitree-K1-SDK",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3.10",
    ],
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.8",
    install_requires=[
        "pyserial",
        "numpy",
        "tqdm",
    ]
)
