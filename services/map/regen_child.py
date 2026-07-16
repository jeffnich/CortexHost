#!/usr/bin/env python3
"""
One-shot regen worker. Runs in a subprocess so the RAM numpy/UMAP grabs is
returned to the OS when it exits; the always-on server process stays tiny
(this is what was costing money: the parent held ~1.4GB 24/7 for 15 min/day
of actual work).

Order: optional retire (RETIRE_SOURCES) -> optional classify of untagged
memories -> pull -> project -> cluster -> render full + demo. Exits 0 on
success; the parent reads /tmp/map.html + /tmp/demo.html after.
"""

import os

import numpy as np

import mapgen

TOPICS = int(os.environ.get("MAP_TOPICS", "50"))
SAMPLE = int(os.environ.get("MAP_SAMPLE", "0"))
OUT = "/tmp/map.html"
DEMO_OUT = "/tmp/demo.html"


def main():
    retire = os.environ.get("RETIRE_SOURCES", "").strip()
    if retire:
        srcs = [s.strip() for s in retire.split(",") if s.strip()]
        try:
            n = mapgen.retire_sources(srcs, log=lambda m: print(m, flush=True))
            print(f"retire: done (~{n} points removed for {srcs})", flush=True)
        except Exception as e:
            print(f"retire error: {type(e).__name__}: {e}", flush=True)
    if os.environ.get("RECLASSIFY", "").strip():
        # one-shot: wipe cached verdicts so the classifier reruns against the
        # CURRENT CORTEX_EXCLUDE (set RECLASSIFY=1, deploy, then unset)
        import requests
        requests.post(f"{mapgen.URL}/collections/{mapgen.COLLECTION}/points/payload/delete?wait=true",
                      json={"keys": ["demo_personal"],
                            "filter": {"must": [{"key": "user_id", "match": {"value": mapgen.SCOPED_USER}}]}},
                      headers={"api-key": mapgen.KEY}, timeout=300).raise_for_status()
        print("reclassify: cleared cached demo_personal verdicts", flush=True)
    if os.environ.get("OPENAI_API_KEY"):
        try:
            mapgen.classify_untagged(log=lambda m: print(m, flush=True))
        except Exception as e:
            print(f"classify error: {type(e).__name__}: {e}", flush=True)
    vecs, texts, sources, types, dates, tss, personal = mapgen.pull()
    if SAMPLE and len(texts) > SAMPLE:
        idx = np.random.RandomState(42).choice(len(texts), SAMPLE, replace=False)
        idx.sort()
        vecs = vecs[idx]
        texts = [texts[i] for i in idx]
        sources = [sources[i] for i in idx]
        types = [types[i] for i in idx]
        dates = [dates[i] for i in idx]
        tss = [tss[i] for i in idx]
        personal = [personal[i] for i in idx]
    norm = mapgen.normalize(mapgen.project(vecs))
    labels, k = mapgen.cluster(norm, TOPICS)
    names = mapgen.label_clusters(texts, labels, k)
    mapgen.build_html(norm, texts, sources, types, dates, tss, labels, names, OUT,
                      personal=personal, demo=False)
    mapgen.build_html(norm, texts, sources, types, dates, tss, labels, names, DEMO_OUT,
                      personal=personal, demo=True)
    n_pub = sum(1 for p in personal if not p)
    print(f"regen child done: {len(texts)} points, {k} topics, {n_pub} public in demo", flush=True)


if __name__ == "__main__":
    main()
