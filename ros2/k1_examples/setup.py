from setuptools import setup

package_name = "k1_examples"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Jason Tang",
    maintainer_email="jason.tang@berkeley.edu",
    description="Example ROS 2 programs for the Unitree K1 arm, using its trajectory and gripper actions",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "move = k1_examples.move:main",
            "gripper = k1_examples.gripper:main",
            "zero = k1_examples.zero:main",
            "demo = k1_examples.demo:main",
        ],
    },
)
