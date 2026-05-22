from setuptools import setup, find_packages

setup(
    name="android-rom-extractor",
    version="0.1.0",
    description="Extract and flash full ROM backups from Android devices.",
    author="at0m",
    python_requires=">=3.9",
    packages=find_packages(),
    install_requires=[
        "rich>=13.0",
        "click>=8.1",
        "customtkinter>=5.2",
    ],
    entry_points={
        "console_scripts": [
            "arom = rom_extractor.cli:main",
            "arom-gui = rom_extractor.gui:run",
        ],
    },
)
