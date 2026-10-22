# web/assets/vendor

Third-party files served as-is to the browser. Nothing here is loaded from a CDN at runtime; every file is committed and pinned.

## three.module.min.js

| | |
|---|---|
| Library | three.js |
| Version | 0.160.0 (r160) |
| Source | https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js |
| sha256 | `3e690ac7d180b0aadf0891bea39eec643e29e2d3e75c99b18689518665f69ba6` |
| License | MIT — full text in `three.LICENSE` (the file also keeps its own `@license` header) |
| Used by | `web/home3d.html` via `web/assets/home3d.js` (dynamic `import()`) |

Single-file ES module build with no imports of its own. No addons (OrbitControls, loaders) are vendored; camera rotation is implemented by hand in `home3d.js`.

To verify the pinned file:

```sh
shasum -a 256 web/assets/vendor/three.module.min.js
```

To upgrade, replace the file from the matching versioned URL, update the version and sha256 above in the same commit, and re-run the browser check described in `web/AGENTS.md`.
