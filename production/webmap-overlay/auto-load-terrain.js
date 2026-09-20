// Adds a default terrain layer when the page URL carries terrain_url /
// terrain_name / terrain_tooltip. app.py only sets these when the configured
// database SRID matches the terrain's native CRS (see _webmap_src in app.py).
(function () {
    var params = new URLSearchParams(window.location.search);
    var terrainUrl = params.get("terrain_url");
    if (!terrainUrl) return;

    function whenReady(callback) {
        if (typeof addTerrainViewModel === "undefined" || typeof addTerrainProvider !== "function") {
            setTimeout(function () { whenReady(callback); }, 200);
            return;
        }
        callback();
    }

    whenReady(function () {
        addTerrainViewModel.url = terrainUrl;
        addTerrainViewModel.name = params.get("terrain_name") || "";
        addTerrainViewModel.tooltip = params.get("terrain_tooltip") || "";
        addTerrainViewModel.iconUrl = "";
        addTerrainProvider();
        console.log("[auto-load-terrain] added default terrain layer:", terrainUrl);
    });
})();
