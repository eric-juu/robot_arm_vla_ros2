from glob import glob

from setuptools import setup

package_name = "k1_web"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
        ("share/" + package_name + "/static", glob("static/*")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Jason Tang",
    maintainer_email="jason.tang@berkeley.edu",
    description="Web interface for remote control of the Unitree K1 arm, with a live camera feed",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "web_node = k1_web.web_node:main",
            "mock_arm = k1_web.mock_arm_node:main",
        ],
    },
)
