"""Fetch the ten pinned public JPX board-lot sources into a local data directory."""

import argparse
import hashlib
import urllib.request
from pathlib import Path

XLS = "https://www.jpx.co.jp/equities/improvements/unit/um3qrc000001s91k-att/nlsgeu000001nx7a.xls"
XLS_SHA = "996a1f70161b0774f0e6ae7f2c7e9332238ab7be1860aa3d50c6ae7b924cfed9"
PDF = "https://www.jpx.co.jp/english/markets/statistics-equities/price/b5b4pj000001dldf-att/"
PDF_DIGESTS = (
    "b981698680c8d49989d434907ee187b648e32b743bfc1266876430dcbeeb7121",
    "c1543542db8ff6031b561f2464ca1af33350a3d7045898d4b3ff9a9375ed74d7",
    "30a0dbe3f940382bc9593c685f142b3c9ade03b3416ef0da0532526ff0580960",
    "8912a9b30da432b6609d4d2802edd8dcee4b4dc889898614d97d3e7f5594a986",
    "4ccb0687914dd5f87139b4ea62e4e389fca81d6a92d6658babd73745b5c7b128",
    "d5fa70ce052d57e856105f60514937f54d9b0327156d87face95155a0dfb8265",
    "411744c039aa418668a465f680cf83ec79245fd78d72ae4edd82871b761ca743",
    "7b41f5f2aa53f0ff234d0d344e19e42e290cfec617688aaed26bfa3c68e350a5",
    "1eba2fa9d0bdb73666f2218b05d18b154b31433eddef24b554a8aa854fe25799",
)


def fetch(folder):
    folder.mkdir(parents=True, exist_ok=True)
    sources = [("jpx_unit_changes_20181001.xls", XLS, XLS_SHA)]
    for i, digest in enumerate(PDF_DIGESTS, 1):
        name = f"st{i}_201611.pdf"
        sources.append((name, PDF + name, digest))
    for name, url, expected in sources:
        path = folder / name
        if path.exists():
            content = path.read_bytes()
        else:
            with urllib.request.urlopen(url, timeout=45) as response:
                content = response.read()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"Source digest mismatch: {name}")
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    fetch(parser.parse_args().out)
