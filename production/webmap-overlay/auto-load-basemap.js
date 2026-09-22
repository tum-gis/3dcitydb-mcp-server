// Adds a default WMS base map when the page URL carries wms_url / wms_layer
// (+ optional wms_name / wms_tooltip / wms_proxy_url). app.py sets these from
// VIEWER_WMS_* env vars (see .env.example), defaulting to basemap.de's
// Germany-wide WMS — configurable for another country.
//
// Drives the same globals the client's own "Add WMS/WMTS Layer" panel uses
// (addWmsViewModel + addWebMapServiceProvider()), so this behaves exactly as
// if the user had filled in that panel by hand: the layer is pushed into the
// BaseLayerPicker's imagery list and selected as the active base map.
(function () {
    var params = new URLSearchParams(window.location.search);
    var wmsUrl = params.get("wms_url");
    var wmsLayer = params.get("wms_layer");
    if (!wmsUrl || !wmsLayer) return;

    function whenReady(callback) {
        if (typeof addWmsViewModel === "undefined" || typeof addWebMapServiceProvider !== "function") {
            setTimeout(function () { whenReady(callback); }, 200);
            return;
        }
        callback();
    }

    whenReady(function () {
        addWmsViewModel.imageryType = "wms";
        addWmsViewModel.url = wmsUrl;
        addWmsViewModel.layers = wmsLayer;
        addWmsViewModel.name = params.get("wms_name") || "";
        addWmsViewModel.tooltip = params.get("wms_tooltip") || "";
        addWmsViewModel.iconUrl = "";
        addWmsViewModel.additionalParameters = "";
        // Only set when the query string actually carries it (basemap.de needs
        // none — it sends Access-Control-Allow-Origin: *); an empty string
        // here means addWebMapServiceProvider() skips the proxy entirely.
        addWmsViewModel.proxyUrl = params.get("wms_proxy_url") || "";
        addWebMapServiceProvider();
        console.log("[auto-load-basemap] added default WMS base map:", wmsUrl, wmsLayer);
    });
})();
