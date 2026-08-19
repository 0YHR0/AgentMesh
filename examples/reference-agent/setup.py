from setuptools import find_packages, setup

setup(
    name="agentmesh-reference-agent",
    version="0.1.0a1",
    packages=find_packages("src"),
    package_dir={"": "src"},
)
