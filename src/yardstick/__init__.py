"""The yardstick (#390): one fixed benchmark, recorded at the end of every epic.

`workload.yaml` (resources/yardstick/) fixes what is measured; `runner` measures
it; `result` is the schema and validator every committed result must pass
(docs/spec/yardstick.md). Run it with `pixi run yardstick`.
"""
