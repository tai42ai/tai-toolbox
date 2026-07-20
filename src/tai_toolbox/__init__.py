"""tai-toolbox — the reference contrib package for the TAI ecosystem.

An opt-in, manifest-loaded collection of generic tools and **tool extensions**
(a plugin extends the platform; a tool extension extends a single tool). Nothing
here is imported at package import time: each module registers its tool or tool
extension through the global ``tai_app`` handle from ``tai_contract.app`` and is
loaded by the host via the manifest's ``tools[].module`` / ``extensions_modules``
fields. Heavy backing dependencies are opt-in extras; a module whose extra is
missing fails loudly with an install hint.
"""
