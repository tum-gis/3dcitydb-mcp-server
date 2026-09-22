// Bridge between the Gradio chat UI (parent frame) and the 3DCityDB Web Map Client.
//
// Parent -> viewer messages (same origin only):
//   {type: "highlight", buildings: [{gmlid, tile_ids: [...]}],
//    centroid: {lat, long, radius_m, height_m} | null}
//   {type: "clear"}
//   {type: "reload_tiles"}
//
// `tile_ids` are the objectids that actually exist in the tileset (the server
// expands e.g. a Room to its boundary surfaces), so styling matches on those.
// While a highlight is active every other feature is either ghosted (default)
// or hidden ("isolate"), because an interior room is otherwise hidden inside
// the building shell.
//
// The viewer -> parent direction (a user's click selection, sent up to the
// chat) is handled by select-bridge.js, loaded after this file; it drives
// the selection styling (cyan) through window._citydbBridge below, kept
// independent of the highlight (orange) so an agent answer never clears a
// selection and vice versa.
(function () {
    "use strict";

    var ORIGIN = window.location.origin;
    // citydb-3dtiler (0.9.4, pg2b3dm 2.26) exposes feature.objectid as the glTF
    // structural-metadata property `id`; other tilers use `objectid` / `GMLID`.
    // Referencing an undefined property in a Cesium3DTileStyle evaluates to
    // undefined (it does not throw), so all are tried.
    var ID_PROPERTIES = ["objectid", "id", "GMLID", "gmlid"];
    var HIGHLIGHT_COLOR = "color('#ff5a00', 1.0)";
    var SELECTION_COLOR = "color('#e11d48', 1.0)";
    var GHOST_COLOR = "color('white', 0.15)";
    var POLL_INTERVAL_MS = 500;
    var MAX_POLL_ATTEMPTS = 60;

    var mode = "ghost"; // "ghost" | "isolate"
    // Last highlight message, re-applied on mode toggle only — never persisted or restored
    // across a reload: the only reload trigger in this app is "the data just changed", so a
    // stale highlight (wrong ids, wrong camera target) must never survive it.
    var last = null;
    var pollTimer = null;
    // The current viewer selection (select-bridge.js), kept separately from
    // `last`: an agent answer highlights in orange without touching a
    // selection the user made in cyan, and vice versa.
    var selectionIds = [];

    // ── tileset access ───────────────────────────────────────────────────────
    // Look in the Cesium scene rather than the client's private layer fields:
    // works for any number of tilesets (the tiler can split by object class).
    function getTilesets() {
        if (typeof cesiumViewer === "undefined" || !cesiumViewer.scene) return [];
        var prims = cesiumViewer.scene.primitives;
        var out = [];
        for (var i = 0; i < prims.length; i++) {
            var p = prims.get(i);
            if (p instanceof Cesium.Cesium3DTileset && p.ready !== false) out.push(p);
        }
        return out;
    }

    // ── style ────────────────────────────────────────────────────────────────
    function escapeRegex(s) {
        return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    // One regular expression over all ids instead of one condition per id.
    function buildMatchExpression(ids) {
        var pattern = "^(" + ids.map(escapeRegex).join("|") + ")$";
        // Cesium style strings: backslashes and quotes must be escaped again.
        var literal = "'" + pattern.replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
        return ID_PROPERTIES.map(function (prop) {
            return "regExp(" + literal + ").test(String(${" + prop + "}))";
        }).join(" || ");
    }

    // Single style owner for both the agent's highlight (orange) and the
    // user's viewer selection (cyan) — they must be able to coexist and stay
    // visually distinct. Selection wins where a feature is in both sets.
    function buildStyle(highlightIds, selIds) {
        var selMatch = selIds.length ? buildMatchExpression(selIds) : null;
        var hlMatch = highlightIds.length ? buildMatchExpression(highlightIds) : null;
        if (mode === "isolate") {
            var anyIds = (selIds || []).concat(highlightIds || []);
            var show = anyIds.length ? buildMatchExpression(anyIds) : "false";
            var isolateConditions = [];
            if (selMatch) isolateConditions.push([selMatch, SELECTION_COLOR]);
            isolateConditions.push(["true", HIGHLIGHT_COLOR]);
            return new Cesium.Cesium3DTileStyle({
                show: show,
                color: selMatch ? { conditions: isolateConditions } : HIGHLIGHT_COLOR,
            });
        }
        var ghostConditions = [];
        if (selMatch) ghostConditions.push([selMatch, SELECTION_COLOR]);
        if (hlMatch) ghostConditions.push([hlMatch, HIGHLIGHT_COLOR]);
        ghostConditions.push(["true", GHOST_COLOR]);
        return new Cesium.Cesium3DTileStyle({ color: { conditions: ghostConditions } });
    }

    // Re-applies the combined style from whatever highlight/selection ids are
    // currently known. Never touches the camera.
    function applyCombinedStyle() {
        var tilesets = getTilesets();
        if (!tilesets.length) return;
        var highlightIds = last ? collectIds(last) : [];
        if (!highlightIds.length && !selectionIds.length) {
            tilesets.forEach(function (ts) {
                ts.style = undefined;
                ts.colorBlendMode = Cesium.Cesium3DTileColorBlendMode.HIGHLIGHT;
            });
            return;
        }
        try {
            var style = buildStyle(highlightIds, selectionIds);
            tilesets.forEach(function (ts) {
                // By default Cesium multiplies the style colour with the model's own
                // colour (orange x a blue window = green). Replace it instead, so a
                // highlight/selection colour is always exact.
                ts.colorBlendMode = Cesium.Cesium3DTileColorBlendMode.REPLACE;
                ts.style = style;
            });
        } catch (err) {
            console.error("[highlight-bridge] could not build tile style:", err);
        }
    }

    function collectIds(message) {
        var ids = {};
        (message.buildings || []).forEach(function (b) {
            var list = (b.tile_ids && b.tile_ids.length) ? b.tile_ids : [];
            list.forEach(function (id) { ids[String(id)] = true; });
        });
        return Object.keys(ids);
    }

    // ── camera ───────────────────────────────────────────────────────────────
    function rad(deg) { return Cesium.Math.toRadians(deg); }

    // Distance at which a sphere of `radius` fills the *smaller* of the viewer's
    // two fields of view, times `margin` — so the whole target is in frame
    // whatever the shape of the embedded panel.
    function fitRange(radius, margin) {
        var f = cesiumViewer.camera.frustum;
        var fovy = f.fovy || rad(60);
        var aspect = f.aspectRatio ||
            (cesiumViewer.canvas.clientWidth / cesiumViewer.canvas.clientHeight) || 1;
        var fovx = 2 * Math.atan(Math.tan(fovy / 2) * aspect);
        return radius / Math.sin(Math.min(fovx, fovy) / 2) * margin;
    }

    // o: {heading, pitch (rad), margin, minRange (m), duration (s)}
    function flyToSphere(center, radius, o) {
        o = o || {};
        var range = Math.max(fitRange(radius, o.margin || 1.4), o.minRange || 0);
        cesiumViewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(center, radius), {
            offset: new Cesium.HeadingPitchRange(
                o.heading || 0, o.pitch != null ? o.pitch : rad(-35), range
            ),
            duration: o.duration != null ? o.duration : 2.0,
        });
    }

    // Camera heading that looks at the target from *outside* its building: the
    // camera sits on the far side of the feature from the building's centre
    // (`anchor`) and looks toward it. Without an anchor (several buildings, or
    // the building itself) there is no single "outside" — face north.
    function headingFor(c) {
        if (!c.anchor) return 0;
        var dx = (c.anchor.long - c.long) * Math.cos(rad(parseFloat(c.lat))); // east
        var dy = c.anchor.lat - c.lat;                                        // north
        if (Math.sqrt(dx * dx + dy * dy) * 111320 < 1.5) return 0;
        return Math.atan2(dx, dy);
    }

    function flyToTarget(c, tilesets) {
        if (!c || c.lat == null || c.long == null) return;
        var radius = Math.max(parseFloat(c.radius_m) || 20, 3);
        var height = c.height_m;
        if (height == null && tilesets.length) {
            // No z from the server: use the tileset's own altitude.
            height = Cesium.Cartographic.fromCartesian(tilesets[0].boundingSphere.center).height;
        }
        var center = Cesium.Cartesian3.fromDegrees(
            parseFloat(c.long), parseFloat(c.lat), height || 0
        );
        // Small targets (a window, a door, a room) get a low, nearly horizontal
        // view straight onto the façade; larger ones (a building, a group) a
        // steeper one that shows more context. Never closer than 35 m.
        var small = radius < 25;
        flyToSphere(center, radius, {
            heading: headingFor(c),
            pitch: rad(small ? -20 : -35),
            margin: small ? 2.0 : 1.5,
            minRange: 35,
        });
    }

    // ── on-page controls ─────────────────────────────────────────────────────
    var panel = null, badge = null, modeBtn = null;

    function ensurePanel() {
        if (panel) return;
        panel = document.createElement("div");
        panel.style.cssText =
            "position:absolute;top:8px;right:8px;z-index:9999;display:none;gap:6px;" +
            "align-items:center;background:rgba(30,41,59,.9);color:#f8fafc;" +
            "padding:6px 10px;border-radius:6px;font:12px/1.4 sans-serif;";
        badge = document.createElement("span");
        modeBtn = document.createElement("button");
        var clearBtn = document.createElement("button");
        [modeBtn, clearBtn].forEach(function (b) {
            b.style.cssText = "cursor:pointer;font:12px sans-serif;padding:2px 8px;";
        });
        clearBtn.textContent = "Clear";
        modeBtn.onclick = function () {
            mode = mode === "ghost" ? "isolate" : "ghost";
            applyCombinedStyle();
        };
        clearBtn.onclick = function () { applyClear(); };
        panel.appendChild(badge);
        panel.appendChild(modeBtn);
        panel.appendChild(clearBtn);
        document.body.appendChild(panel);
    }

    function showPanel(message, ids) {
        ensurePanel();
        var text = ids.length + " feature" + (ids.length === 1 ? "" : "s") + " highlighted";
        var missing = (message.missing || []).length;
        var noGeom = (message.not_tileable || []).length;
        if (missing) text += " · " + missing + " not found";
        if (noGeom) text += " · " + noGeom + " without geometry";
        badge.textContent = text;
        modeBtn.textContent = mode === "ghost" ? "Isolate" : "Show context";
        panel.style.display = "flex";
    }

    function warn(message) {
        ensurePanel();
        badge.textContent = message;
        modeBtn.style.display = "none";
        panel.style.display = "flex";
    }

    // ── actions ──────────────────────────────────────────────────────────────
    function applyHighlight(message, fly) {
        var tilesets = getTilesets();
        if (!tilesets.length) {
            last = message;
            startPoll();
            return;
        }
        var ids = collectIds(message);
        last = message;
        try {
            applyCombinedStyle();
        } catch (err) {
            console.error("[highlight-bridge] could not build tile style:", err);
            warn("Highlight failed: " + err);
            return;
        }
        console.log("[highlight-bridge] highlighting", ids.length, "features, e.g.", ids[0]);
        modeBtn && (modeBtn.style.display = "");
        showPanel(message, ids);
        if (ids.length && fly !== false) flyToTarget(message.centroid, tilesets);
    }

    // Clears the agent's highlight only — a viewer selection (cyan) survives
    // an answer, and Clear here never touches selectionIds.
    function applyClear() {
        last = null;
        applyCombinedStyle();
        if (panel) panel.style.display = "none";
    }

    // Reload = reload this page. auto-load-tileset.js re-fetches the tileset,
    // and the server sends `Cache-Control: no-cache` for /tiles so a refreshed
    // tileset is never served stale. The reload always means new/changed data,
    // so nothing is carried across it — `last` starts null on every fresh load.
    function applyReload() {
        window.location.reload();
    }

    // Wait for the auto-loaded tileset, then apply a highlight that arrived early.
    function startPoll() {
        if (pollTimer) return;
        var attempts = 0;
        pollTimer = setInterval(function () {
            attempts++;
            if (getTilesets().length) {
                clearInterval(pollTimer);
                pollTimer = null;
                if (last) applyHighlight(last, true);
            } else if (attempts >= MAX_POLL_ATTEMPTS) {
                clearInterval(pollTimer);
                pollTimer = null;
                console.error("[highlight-bridge] gave up waiting for a tileset");
                warn("3D tileset not loaded — generate tiles first (Import tab or Recreate 3D Tiles).");
            }
        }, POLL_INTERVAL_MS);
    }

    window.addEventListener("message", function (event) {
        // Only the embedding Gradio page (same origin) may drive the viewer.
        if (event.origin !== ORIGIN || event.source !== window.parent) return;
        var data = event.data;
        if (!data || typeof data !== "object") return;
        if (data.type === "reload_tiles") applyReload();
        else if (data.type === "clear") applyClear();
        else if (data.type === "highlight") applyHighlight(data, true);
    });

    // ── shared bridge (used by select-bridge.js) ────────────────────────────
    // Sets the viewer selection (cyan) independently of the agent's highlight
    // (orange) — never flies the camera, since the user is already looking
    // at whatever they selected.
    function setSelection(ids) {
        selectionIds = (ids || []).map(String);
        applyCombinedStyle();
    }

    window._citydbBridge = {
        getTilesets: getTilesets,
        buildMatchExpression: buildMatchExpression,
        ID_PROPERTIES: ID_PROPERTIES,
        setSelection: setSelection,
    };

    // Embedded in the chat page, the client's "Introduction" splash window would
    // cover the whole map. script.js builds it synchronously before this file
    // runs, so it can be closed right away. Opened standalone, it still shows.
    if (window.parent !== window && typeof splashController !== "undefined") {
        try {
            splashController.closeSplashWindow(jQuery);
            // The client opens its navigation-help dropdown (About tab) with the
            // splash; keep it closed so it does not cover the small embedded map.
            if (cesiumViewer.navigationHelpButton) {
                cesiumViewer.navigationHelpButton.viewModel.showInstructions = false;
            }
        } catch (e) { console.warn("[highlight-bridge] could not close the splash window:", e); }
    }
})();
