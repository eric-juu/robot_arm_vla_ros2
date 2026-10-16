from glob import glob

from setuptools import setup

package_name = "k1_driver_py"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Jason Tang",
    maintainer_email="jason.tang@berkeley.edu",
    description="ROS 2 driver node for the Unitree K1 arm, wrapping unitree_k1_sdk",
    license="MIT",
    entry_points={
        "console_scripts": [
            "k1_driver = k1_driver_py.k1_driver_node:main",
        ],
    },
)
