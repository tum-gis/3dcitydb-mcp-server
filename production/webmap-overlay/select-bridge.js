// Click-to-select bridge: lets a user build a multi-feature selection in the
// 3D Web Map Client and send it up to the chat UI (viewer -> parent).
//
// Loaded after highlight-bridge.js, which owns the combined tile style
// (window._citydbBridge) so the selection (red) and the agent's highlight
// (orange) can coexist without clobbering each other.
//
// Viewer -> parent messages (same origin only):
//   {type: "selection", features: [{objectid, classname}, ...], count}
//
// Browse, then add — a 3D click never mutates the selection by itself (see
// the comment above onClick for why). It picks a Cesium3DTileFeature —
// usually a boundary surface (a wall, a roof), not the CityGML feature the
// user means — and the server's containment tree (GET /api/feature-tree)
// shows what that feature belongs to (its ancestors, e.g. the Building) and
// what it contains (its children, e.g. Windows) or shares a parent with
// (its siblings, e.g. other surfaces of the same building). Each row has
// its own [+], so adding the Building (or any other row) to the selection
// is one click there; "Next selection" just clears the browsed tree so the
// panel is visibly ready for the next pick — it never touches the selection.
(function () {
    "use strict";

    var ORIGIN = window.location.origin;
    var MAX_TREE_FETCH_MS = 8000;

    // objectid -> {objectid, classname} — insertion order is display order.
    var selection = {};
    var selecting = false;
    var clickHandler = null;
    // Guards the toggle button: selecting before there's anything to click
    // on isn't useful, so the toggle stays disabled ("waiting for tiles…")
    // until the tileset has actually loaded.
    var tilesetReady = false;

    function bridge() { return window._citydbBridge; }

    // ── networking ───────────────────────────────────────────────────────────
    function getJSON(url) {
        var controller = ("AbortController" in window) ? new AbortController() : null;
        var opts = controller ? { signal: controller.signal } : {};
        var timer = controller ? setTimeout(function () { controller.abort(); }, MAX_TREE_FETCH_MS) : null;
        return fetch(url, opts).then(function (res) {
            if (timer) clearTimeout(timer);
            if (!res.ok) throw new Error("HTTP " + res.status);
            return res.json();
        }).then(function (data) {
            if (data && data.error) throw new Error(data.error);
            return data;
        });
    }

    function fetchFeatureTree(objectid) {
        return getJSON("/api/feature-tree?objectid=" + encodeURIComponent(objectid));
    }

    function fetchSelectionInfo(objectids) {
        if (!objectids.length) {
            return Promise.resolve({ features: [], count: 0, by_classname: {}, missing: [], truncated: false });
        }
        var qs = objectids.map(function (id) { return "objectid=" + encodeURIComponent(id); }).join("&");
        return getJSON("/api/selection-info?" + qs);
    }

    // ── picking ──────────────────────────────────────────────────────────────
    function pickedFeatureId(movement) {
        var picked = cesiumViewer.scene.pick(movement.position);
        if (!picked || typeof picked.getProperty !== "function") return null;
        var ids = (bridge() && bridge().ID_PROPERTIES) || ["objectid", "id", "GMLID", "gmlid"];
        for (var i = 0; i < ids.length; i++) {
            var v = picked.getProperty(ids[i]);
            if (v != null) return String(v);
        }
        return null;
    }

    // A 3D click ONLY browses — it loads and shows the containment tree for
    // whatever was picked, and never mutates the selection by itself.
    //
    // Two earlier approaches both broke on real clicks: relying on Cesium's
    // built-in Shift-modified LEFT_CLICK registration (a second, separate
    // action keyed by Cesium.KeyboardEventModifier.SHIFT) silently dropped
    // shift-clicks; tracking Shift ourselves and auto-adding on every click
    // didn't fix it, because the actual cause is Cesium's click-vs-drag
    // pixel-tolerance check (~5px) upstream of any of our own code — holding
    // Shift changes hand mechanics enough that real shift-clicks routinely
    // move more than that between mousedown and mouseup, so Cesium never
    // fires LEFT_CLICK at all for that press, for any listener.
    //
    // So selection no longer depends on a 3D click succeeding as a "click":
    // browsing (this handler) is a plain, unmodified LEFT_CLICK — reliable,
    // since it doesn't need Shift held — and adding to the selection is a
    // plain DOM button click ("Add to selection" below, or a row's own
    // [+]), which doesn't touch Cesium's picking/gesture logic at all.
    function onClick(movement) {
        var id = pickedFeatureId(movement);
        if (id) openTree(id);
    }

    // ── selection state ──────────────────────────────────────────────────────
    // The tree panel's own content (ancestors/children/siblings) doesn't
    // change when the selection set changes — only the per-row [+]/[−]
    // labels and the summary count do. Re-render from the last loaded tree
    // instead of re-fetching.
    var lastTree = null;

    // Every mutation posts the new selection up to the parent chat page
    // immediately — the chat-side badge must reflect what's on screen the
    // instant the user clicks, not only once they press a separate button.
    function addToSelection(objectid, classname) {
        if (!selection[objectid]) selection[objectid] = { objectid: objectid, classname: classname || null };
        else if (classname) selection[objectid].classname = classname;
        syncSelectionStyle();
        renderPanel(lastTree);
        postSelection();
    }

    function removeFromSelection(objectid) {
        delete selection[objectid];
        syncSelectionStyle();
        renderPanel(lastTree);
        postSelection();
    }

    function toggleSelection(objectid, classname) {
        if (selection[objectid]) removeFromSelection(objectid);
        else addToSelection(objectid, classname);
    }

    function clearSelection() {
        selection = {};
        lastTree = null; // drop the tree too — Clear means "start over", not "keep browsing"
        syncSelectionStyle();
        renderPanel(null);
        postSelection();
    }

    // Re-resolves the current selection to tileable ids and re-applies the
    // red style — never moves the camera (the user is already looking at
    // whatever is selected).
    function syncSelectionStyle() {
        var ids = Object.keys(selection);
        fetchSelectionInfo(ids).then(function (info) {
            var tileIds = {};
            (info.features || []).forEach(function (f) {
                (f.tile_ids || []).forEach(function (t) { tileIds[String(t)] = true; });
                if (selection[f.objectid]) selection[f.objectid].classname = f.classname;
            });
            if (bridge()) bridge().setSelection(Object.keys(tileIds));
        }).catch(function (err) {
            console.warn("[select-bridge] selection-info failed:", err);
        });
    }

    // ── posting up to the parent chat page ──────────────────────────────────
    function postSelection() {
        var features = Object.keys(selection).map(function (id) {
            return { objectid: id, classname: selection[id].classname || null };
        });
        window.parent.postMessage(
            { type: "selection", features: features, count: features.length },
            ORIGIN
        );
    }

    // ── select-mode toggle & the picking handler ─────────────────────────────
    function setSelecting(on) {
        selecting = on;
        if (!clickHandler) {
            clickHandler = new Cesium.ScreenSpaceEventHandler(cesiumViewer.scene.canvas);
        }
        if (on) {
            clickHandler.setInputAction(onClick, Cesium.ScreenSpaceEventType.LEFT_CLICK);
        } else {
            clickHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_CLICK);
        }
        // Best-effort: suppress the stock client's own info box while
        // selecting, so it doesn't cover the tree panel. The client's click
        // handler (registered in its own script.js, not in this repo) still
        // fires — we only hide its result, we do not intercept it.
        try {
            if (cesiumViewer.infoBox && cesiumViewer.infoBox.container) {
                cesiumViewer.infoBox.container.style.display = on ? "none" : "";
            }
        } catch (e) { /* stock client's InfoBox shape may differ; non-fatal */ }
        renderPanel(lastTree);
    }

    // ── tree panel ───────────────────────────────────────────────────────────
    function openTree(objectid) {
        fetchFeatureTree(objectid).then(function (tree) {
            lastTree = tree;
            renderPanel(tree);
        }).catch(function (err) {
            console.warn("[select-bridge] feature-tree failed:", err);
            renderPanel(lastTree, err);
        });
    }

    // One persistent panel, visible from page load — not just after a pick.
    // An earlier version used a separate floating toggle button that got
    // hidden once the tree panel appeared, which left no visible way to
    // tell selection mode was on, or to turn it off. Everything now lives
    // in this one panel so there is always a single, obvious place to look.
    //
    // Collapsed by default: the full tree/summary/action body takes real
    // screen space, and most users won't want to select anything the
    // instant the viewer loads. Collapsed, only the compact header row
    // shows (title, selecting on/off, a count badge, the expand arrow) at
    // the same bottom-left spot — selecting stays fully usable while
    // collapsed, so someone can turn it on, click around in the 3D view,
    // and only expand later to see what they picked.
    var panel = null, toggleBtn = null, treeBody = null, nextBtn = null, finishBtn = null,
        summaryEl = null, countBadge = null, collapseBtn = null, actionsRow = null;
    var collapsed = true;

    function ensurePanel() {
        if (panel) return;
        panel = document.createElement("div");
        // Bottom-left, clear of the stock client's own top-left show/hide
        // toolbox button — top-left overlapped it and got partially hidden.
        // 34px clears the client's bottom credit/attribution bar.
        panel.style.cssText =
            "position:absolute;bottom:34px;left:8px;z-index:9999;display:flex;flex-direction:column;" +
            "gap:6px;max-width:280px;background:rgba(30,41,59,.94);color:#f8fafc;" +
            "padding:8px 10px;border-radius:6px;font:12px/1.5 sans-serif;max-height:60%;" +
            "box-shadow:0 1px 6px rgba(0,0,0,.5);";

        var header = document.createElement("div");
        header.style.cssText = "display:flex;align-items:center;gap:6px;flex-shrink:0;";
        var title = document.createElement("div");
        title.textContent = "Selection";
        title.style.cssText = "font-weight:600;";
        toggleBtn = document.createElement("button");
        toggleBtn.style.cssText =
            "cursor:pointer;font:11px sans-serif;font-weight:600;padding:3px 8px;border-radius:5px;border:none;";
        toggleBtn.onclick = function () { if (tilesetReady) setSelecting(!selecting); };
        countBadge = document.createElement("span");
        countBadge.style.cssText = "font-size:11px;opacity:.8;margin-left:auto;";
        collapseBtn = document.createElement("button");
        collapseBtn.title = "Show/hide the selection panel";
        collapseBtn.style.cssText =
            "cursor:pointer;font:12px sans-serif;padding:2px 7px;border-radius:5px;border:none;" +
            "background:#64748b;color:#f8fafc;";
        collapseBtn.onclick = function () {
            collapsed = !collapsed;
            renderPanel(lastTree);
        };
        header.appendChild(title);
        header.appendChild(toggleBtn);
        header.appendChild(countBadge);
        header.appendChild(collapseBtn);
        panel.appendChild(header);

        // Its own scroll region, sized to whatever the panel has left — a
        // flex column with overflow on the *panel* lets children get
        // squeezed down to near-zero height before scrolling ever kicks in
        // (a well-known flex/overflow quirk), which is why the Add button
        // used to render as a squashed, textless sliver. Scrolling only
        // the tree, with everything below it (Add, summary, Clear) fixed
        // and flex-shrink:0, keeps those always fully visible instead.
        treeBody = document.createElement("div");
        treeBody.style.cssText = "flex:1 1 auto;min-height:0;overflow:auto;";
        panel.appendChild(treeBody);

        summaryEl = document.createElement("div");
        summaryEl.style.cssText =
            "border-top:1px solid rgba(255,255,255,.2);padding-top:6px;margin-top:2px;flex-shrink:0;";
        panel.appendChild(summaryEl);

        // Adding a feature is already done by a row's own [+] (usually the
        // top-level Building row) — no separate "Add" button duplicating
        // that. "Next" is a different, simpler action: done looking at this
        // one, clear the browsed tree so the panel is obviously ready for
        // the next pick. It never touches the selection.
        actionsRow = document.createElement("div");
        actionsRow.style.cssText = "display:flex;gap:6px;flex-shrink:0;";
        nextBtn = document.createElement("button");
        nextBtn.textContent = "Next selection →";
        nextBtn.style.cssText =
            "cursor:pointer;font:12px sans-serif;font-weight:600;padding:5px 8px;flex:1 1 auto;min-width:0;" +
            "border-radius:5px;border:none;background:#0d9488;color:#f8fafc;display:none;";
        nextBtn.onclick = function () {
            lastTree = null;
            renderPanel(null); // browsing view only — leaves the selection untouched
        };
        actionsRow.appendChild(nextBtn);
        var clearBtn = document.createElement("button");
        clearBtn.textContent = "Clear selection";
        clearBtn.style.cssText = "cursor:pointer;font:12px sans-serif;padding:2px 8px;flex-shrink:0;";
        clearBtn.onclick = clearSelection; // already posts the (now empty) selection
        actionsRow.appendChild(clearBtn);
        panel.appendChild(actionsRow);

        // Explicit "I'm done" step: the selection itself is already synced
        // to the chat (every add/remove posts immediately) — this just
        // turns off select mode, so further clicks in the 3D view don't
        // keep opening new trees, and hands focus to the chat input.
        finishBtn = document.createElement("button");
        finishBtn.textContent = "✓ Finish selecting";
        finishBtn.style.cssText =
            "cursor:pointer;font:12px sans-serif;font-weight:600;padding:6px 8px;flex-shrink:0;" +
            "border-radius:5px;border:none;background:#4f46e5;color:#f8fafc;display:none;";
        finishBtn.onclick = function () {
            lastTree = null;
            if (tilesetReady && selecting) setSelecting(false);
            else renderPanel(null);
            window.parent.postMessage({ type: "selection_done" }, ORIGIN);
        };
        panel.appendChild(finishBtn);

        document.body.appendChild(panel);
        renderToggle();
    }

    function renderToggle() {
        if (!toggleBtn) return;
        // Short label — this sits in the always-visible header row alongside
        // the title, count badge and collapse arrow, so there isn't room for
        // a sentence; the full explanation is the title attribute (tooltip)
        // and, when expanded, the hint text in the tree body.
        if (!tilesetReady) {
            toggleBtn.textContent = "…";
            toggleBtn.title = "Waiting for the 3D tileset to load.";
            toggleBtn.style.background = "#475569";
            toggleBtn.style.cursor = "default";
        } else {
            // Bright teal while active — the affordance that tells the user
            // clicking the 3D view does something; easy to miss otherwise.
            toggleBtn.textContent = selecting ? "ON" : "OFF";
            toggleBtn.title = selecting ? "Selecting is on — click to turn off." : "Click to turn selecting on.";
            toggleBtn.style.background = selecting ? "#0d9488" : "#64748b";
            toggleBtn.style.cursor = "pointer";
        }
        toggleBtn.style.color = "#f8fafc";
    }

    function row(label, classname, objectid, opts) {
        opts = opts || {};
        var r = document.createElement("div");
        r.style.cssText = "display:flex;align-items:center;gap:6px;padding:1px 0;" +
            (opts.indent ? "margin-left:" + (opts.indent * 12) + "px;" : "");

        var text = document.createElement("span");
        text.textContent = (classname ? classname + " " : "") + objectid + (opts.picked ? " ● picked" : "");
        text.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" +
            (opts.picked ? "font-weight:600;" : "cursor:pointer;");
        if (!opts.picked) {
            text.title = "Click to browse from here";
            text.onclick = function () { openTree(objectid); };
        }
        r.appendChild(text);

        var btn = document.createElement("button");
        var inSel = !!selection[objectid];
        btn.textContent = inSel ? "−" : "+";
        btn.title = inSel ? "Remove from selection" : "Add to selection";
        btn.style.cssText = "cursor:pointer;font:11px sans-serif;padding:0 6px;line-height:1.6;";
        btn.onclick = function () { toggleSelection(objectid, classname); }; // toggleSelection already posts
        r.appendChild(btn);

        return r;
    }

    function sectionLabel(text) {
        var d = document.createElement("div");
        d.textContent = text;
        d.style.cssText = "opacity:.7;margin:4px 0 2px;font-size:11px;";
        return d;
    }

    function renderPanel(tree, err) {
        ensurePanel();
        renderToggle();
        treeBody.innerHTML = "";

        if (err) {
            var e = document.createElement("div");
            e.textContent = "Could not load: " + err.message;
            treeBody.appendChild(e);
        } else if (tree) {
            if (!tree.picked) {
                var nf = document.createElement("div");
                nf.textContent = "Feature not found in the database.";
                treeBody.appendChild(nf);
            } else {
                // Ancestors are nearest-parent-first; display top-most first.
                var ancestors = (tree.ancestors || []).slice().reverse();
                ancestors.forEach(function (a, i) {
                    treeBody.appendChild(row(null, a.classname, a.objectid, { indent: i }));
                });
                var pickedIndent = ancestors.length;
                treeBody.appendChild(row(null, tree.picked.classname, tree.picked.objectid,
                    { indent: pickedIndent, picked: true }));

                if ((tree.children || []).length) {
                    treeBody.appendChild(sectionLabel("children"));
                    tree.children.forEach(function (c) {
                        treeBody.appendChild(row(null, c.classname, c.objectid, { indent: pickedIndent + 1 }));
                    });
                    if (tree.children_truncated) treeBody.appendChild(sectionLabel("… truncated"));
                }
                if ((tree.siblings || []).length) {
                    treeBody.appendChild(sectionLabel("siblings of " + tree.picked.classname));
                    tree.siblings.forEach(function (s) {
                        treeBody.appendChild(row(null, s.classname, s.objectid, { indent: pickedIndent }));
                    });
                    if (tree.siblings_truncated) treeBody.appendChild(sectionLabel("… truncated"));
                }
            }
        } else {
            var hint = document.createElement("div");
            if (!tilesetReady) {
                hint.textContent = "Waiting for the 3D tileset to load before selection can start…";
            } else {
                hint.textContent = selecting
                    ? "Click a feature in the 3D view to browse it, then use its + to select it."
                    : "Selection mode is off. Turn it on to click features in the 3D view.";
            }
            hint.style.opacity = ".85";
            treeBody.appendChild(hint);
        }

        // "Next selection" only makes sense once there's something browsed
        // to move on from — and, like the rest of the body, only while expanded.
        nextBtn.style.display = (!collapsed && tree && tree.picked) ? "" : "none";

        var ids = Object.keys(selection);
        // Only worth showing once there's actually something to finish with.
        finishBtn.style.display = (!collapsed && ids.length) ? "" : "none";
        summaryEl.innerHTML = "";
        var summaryTitle = document.createElement("div");
        summaryTitle.style.cssText = "font-weight:600;margin-bottom:2px;";
        summaryTitle.textContent = "Selected: " + ids.length + " feature" + (ids.length === 1 ? "" : "s");
        summaryEl.appendChild(summaryTitle);
        // Id + type for every selected feature — a bare count doesn't say
        // *which* buildings/surfaces are selected, which matters once
        // there's more than one.
        var _selMax = 12;
        ids.slice(0, _selMax).forEach(function (id) {
            var f = selection[id];
            var line = document.createElement("div");
            line.textContent = (f.classname ? f.classname + " " : "") + id;
            line.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;";
            summaryEl.appendChild(line);
        });
        if (ids.length > _selMax) {
            summaryEl.appendChild(sectionLabel("… and " + (ids.length - _selMax) + " more"));
        }

        // Collapsed: only the header row (title, on/off, count, arrow) shows,
        // at the same spot — selecting itself keeps working while collapsed.
        treeBody.style.display = collapsed ? "none" : "";
        summaryEl.style.display = collapsed ? "none" : "";
        actionsRow.style.display = collapsed ? "none" : "flex";
        countBadge.textContent = ids.length ? "(" + ids.length + ")" : "";
        collapseBtn.textContent = collapsed ? "▸" : "▾";
        panel.style.maxHeight = collapsed ? "" : "60%";
    }

    // Off by default — the user turns it on when they actually want to
    // select something, rather than every click in the 3D view doing
    // something unexpected. The toggle itself only needs the tileset to be
    // present (no click handler to register early), so it's enabled as soon
    // as one exists.
    function whenTilesetReady(callback) {
        if (!bridge() || bridge().getTilesets().length === 0) {
            setTimeout(function () { whenTilesetReady(callback); }, 300);
            return;
        }
        callback();
    }
    // Show the panel immediately — "waiting for tiles" is still a visible,
    // honest answer, and better than no panel at all while
    // whenTilesetReady is still polling.
    renderPanel(null);
    whenTilesetReady(function () {
        tilesetReady = true;
        renderPanel(lastTree);
    });
})();
