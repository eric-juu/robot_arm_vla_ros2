from setuptools import setup

package_name = "unitree_k1_sdk"

setup(
    name=package_name,
    version="2024.11.1",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Jason Tang",
    maintainer_email="jason.tang@berkeley.edu",
    description="Serial driver library for the Unitree K1 (Sagittarius) arm",
    license="MIT",
    tests_require=["pytest"],
)
