"""NTFS $MFT / USN-journal ingest plugin for Winnow.

Parses the raw artifacts directly — the $MFT file and the $UsnJrnl:$J
alternate data stream as collected by KAPE/FTK/X-Ways or `icat` — into
Winnow tables, with no external tool in the loop. Pure stdlib Python, so
it works on the same airgapped analysis box Winnow itself targets (an
alternative design would shell out to EZTools' MFTECmd and ingest its CSV,
but then the plugin dies without a .NET runtime installed).

This is also the reference example for the plugin system: a folder
plugin with metadata, two registered ingest formats (extension AND
bare-filename matching — "$MFT"/"$J" have no extension), per-format
options, explicit column_types, and helper modules imported relatively.
To install it, copy this folder into plugins/ (see plugins/README.md).
"""

from . import mft, usn

PLUGIN = {
    "name": "mft-usn",
    "version": "1.0.0",
    "description": "Parse raw NTFS $MFT and USN journal ($J) files directly — no MFTECmd/EZTools needed.",
}

WINNOW_API_VERSION = 1


def register(api):
    api.register_ingest_format(
        id="mft",
        label="NTFS $MFT (raw)",
        extensions=[".mft"],
        filename_patterns=["$MFT", "$MFT.copy*", "*.mft"],
        description=(
            "FILE records from a raw $MFT: full paths reconstructed from parent "
            "references, $STANDARD_INFORMATION and $FILE_NAME timestamps side by "
            "side (SI<FN Created flags the classic timestomp shape), in-use and "
            "deleted records alike."
        ),
        options=[
            {"name": "records", "label": "Records", "type": "choice",
             "choices": ["all", "in-use", "deleted"], "default": "all"},
        ],
        parse=mft.parse,
    )
    api.register_ingest_format(
        id="usn",
        label="NTFS USN journal ($J, raw)",
        extensions=[".usn"],
        filename_patterns=["$J", "*$UsnJrnl*", "$Extend*$J", "*.usn"],
        description=(
            "USN_RECORD v2/v3 entries from a raw $UsnJrnl:$J stream (sparse "
            "leading zeros skipped): timestamp, filename, decoded reason flags, "
            "and MFT entry/sequence numbers that join back against an ingested "
            "$MFT in the SQL pane."
        ),
        parse=usn.parse,
    )
