"""The yardstick (#390): one fixed query workload, run on every opengwasdb release
(#487) and every ogquery release (#506).

`workload.yaml` (resources/yardstick/) fixes what is measured; `runner` measures
it; `result` is the schema and validator every committed result must pass
(docs/spec/yardstick.md). Run it with `pixi run yardstick`.
"""
