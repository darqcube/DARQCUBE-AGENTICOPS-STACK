"""DarqCube automation.

IMPORT RULE — this package must ALWAYS be imported as `automation.<sub>`.

The subpackages are named after the tools they wrap (nornir, textfsm, ttp),
which means they SHADOW the installed libraries of the same name if
`automation/` itself is ever placed on sys.path. Keep only the repository root
(/app in the container) on the path, and import absolutely:

    from automation.nornir import tasks        # correct
    from automation.textfsm import parse       # correct
    import parse                               # WRONG — breaks the real textfsm

The directory names are deliberate: automation/nornir/ holds the Nornir
orchestration, automation/netmiko/ the standalone scripts, automation/textfsm/
the tabular parsing layer, automation/ttp/ the config parsing layer, and
automation/assurance/ the vendor-neutral checks.
"""
