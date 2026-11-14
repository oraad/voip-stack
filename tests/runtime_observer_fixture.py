"""Read the coordinated component's actual observer decision body for host probes."""

from tests.support.sibling_repos import sibling_repo


def source_event_body():
    root = sibling_repo("RUNTIME_CONTROLLER_REPO", "esphome-runtime-controller")
    source = (root / "esphome/components/runtime_controller/runtime_network_observers.cpp").read_text()
    bodies = []
    for signature, guard in (("void RuntimeNetworkObservers::source_event(", None),
                             ("void RuntimeNetworkObservers::publish_api_presence_()", "USE_RUNTIME_CONTROLLER_API")):
        start = source.index(signature)
        body = source[start:source.index("\n}\n", start) + 2]
        bodies.append(f"#ifdef {guard}\n{body}\n#endif\n" if guard else body)
    return "\n".join(bodies)
