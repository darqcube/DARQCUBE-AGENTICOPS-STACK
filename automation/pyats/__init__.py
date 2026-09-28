"""pyATS / Genie assurance — additive, and only where Genie returns data.

What each platform gets is declared in platforms.yml under `pyats:`. A platform
with no block never reaches this package, and every platform keeps its TextFSM
interface checks whether or not it has one.
"""
