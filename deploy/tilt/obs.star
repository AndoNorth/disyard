# Observability wiring for a module's containers, applied by its Tiltfile.module.
# The Tiltfile exports the cluster's observability switch as WFT_OBS, and a service sends telemetry only when it is on,
# so a switch-off service never resolves a name in wft-obs.

def obs_env():
    """Returns the environment a container needs to send traces, logs and profiles to the stack, empty when it is off."""
    if os.getenv("WFT_OBS") != "on":
        return {}
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.wft-obs:4318",
        "WFT_PYROSCOPE_ADDRESS": "http://pyroscope.wft-obs:4040",
    }
