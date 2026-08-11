# Maintenance Policy

Each audit must establish timestamp, working directory, Git state, changed areas, checks run, evidence, and limitations. Findings are prioritized P0-P3 and must separate observed facts from inference.

Quick scans focus on material change and obvious failure. Daily audits cover code/test/config/database health. Weekly reviews assess architecture, duplication, performance, security, operability, and simplification.

Do not rewrite application files during scheduled runs. Persist only reports and maintenance state.

