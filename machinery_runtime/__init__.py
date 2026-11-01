"""The thin runtime of the machinery plugin contract (package machinery.plugin.v1alpha1) for Python.

A plugin is any object with some of these methods; the runtime answers unimplemented for the rest:

    describe(caps) -> Answer          data: name, version, job and verbs (protocol and contracts are added)
    plan(input, caps) -> Answer
    run_unit(input, network, caps) -> Answer
    merge(input, caps) -> Answer
    run(input, network, caps) -> Answer

input is the call's input as plain JSON values; caps is {"timeSeconds", "outputBytes"}; network is
{"hosts": [...]} or {"hosts": [], "replay": {"start", "exchanges"}}, with the defaults filled in. A method
fails by raising Refusal(code, warnings, not_read). serve(plugin) serves it on 127.0.0.1.
"""
from .server import Answer, Refusal, application, serve

__all__ = ["Answer", "Refusal", "application", "serve"]
