"""Strict opt-in Studio V9 manifest and Drive listing validation."""
import re
from collections import Counter

def validate_name(name):
    if (not isinstance(name,str) or not name or name != name.strip()
            or len(name)>255 or name in (".","..")
            or re.search(r'[\\/\x00-\x1f*?\[\]{}]',name)):
        raise ValueError("Unsafe project asset filename")
    return name

def validate_manifest(data):
    folder=(data.get("cloud_export") or {}).get("target_folder_id")
    if not isinstance(folder,str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}",folder):
        raise ValueError("Explicit project Drive folder required")
    slots=data.get("timeline_slots")
    if not isinstance(slots,list) or not slots:
        raise ValueError("Explicit nonempty project timeline required")
    names=set()
    for slot in slots:
        names.add(validate_name((slot.get("base_content") or {}).get("file_name")))
        for insert in slot.get("inserts",[]):
            names.add(validate_name(insert.get("file_name")))
    if len({n.casefold() for n in names}) != len(names):
        raise ValueError("Ambiguous project asset filenames")
    return names

def validate_listing(needed,listing):
    counts=Counter(n.casefold() for n in listing)
    exact=Counter(listing)
    for name in needed:
        if exact[name]!=1 or counts[name.casefold()]!=1:
            raise ValueError("Missing or ambiguous exact Drive asset: "+name)
