# Traffic Pilot Limited Runtime - ApexFabric V1 Intel Delivery

API-only traffic-pilot runtime for Intel 285H limited to people counting, vehicle counting, ANPR, and fire/smoke detection. The image contains no UI, no npm,
and no Vite dashboard. It exposes runtime control and observation endpoints on
`:8080`.

## Runtime

The container starts:

```text
python -m traffic_pilot_runtime.solution_image_entrypoint
```

It reads `/configs/desired_state.json`, validates it against the V1 contract,
accepts only `vehicle_counting`, `pedestrian_counting`, `anpr`/`plate_detection`,
and `fire_smoke_detection`, compiles the active runtime plan, writes
`/plans/traffic-pilot.runtime_plan.json`, generates the worker camera config, and
starts the OpenVINO worker.

## Hot Reload

The runtime checks desired state every 3 seconds. A newer valid revision is
compiled first while the old worker continues running. Only after validation and
config generation succeed does the runtime start the new worker and stop the old
worker. Invalid updates are rejected and the previous worker stays active.

## Required Mounts

```text
/configs/desired_state.json
/run/secrets/apexfabric/*.rtsp
/state
/dev/dri
/dev/accel
```

Camera `source` values in desired state must be secret references such as:

```text
file:/run/secrets/apexfabric/cam1.rtsp
```

The secret file may contain an RTSP/HTTP stream URL or an absolute local video
path mounted into the container.

## Counting Geometry

For each camera/input, counting uses exactly one mode:

```text
line exists      -> line crossing count
else zone exists -> zone count
else             -> whole-frame count
```


## Smoke/fire snapshots

When `fire_smoke_detection` raises a fire or smoke alert, the worker writes a JPEG frame snapshot under `/state/snapshots/<camera_id>/` and includes the snapshot path in the alert payload. The image bytes are not embedded in the event so message payloads stay small for MQTT, Kafka, SSE, and similar sinks.
