import L from 'leaflet';
import {hasHeatPrice, heatDomain, heatRadius, heatRaster} from './price-heat.js';

export class PriceHeatLayer extends L.Layer {
  constructor() {
    super();
    this.points = [];
    this.domain = null;
  }

  onAdd(map) {
    this.map = map;
    let pane = map.getPane('priceHeat');
    if (!pane) {
      pane = map.createPane('priceHeat');
      pane.style.zIndex = '350';
      pane.style.pointerEvents = 'none';
    }
    this.canvas = L.DomUtil.create('canvas', 'price-heat-canvas leaflet-zoom-hide', pane);
    this.canvas.setAttribute('role', 'img');
    this.canvas.setAttribute('aria-label', 'Тепловая карта цен недвижимости');
    this.buffer = document.createElement('canvas');
    this.schedule();
  }

  onRemove() {
    cancelAnimationFrame(this.frame);
    this.canvas.remove();
    this.map = null;
  }

  getEvents() {
    return {moveend: this.schedule, zoomend: this.schedule, resize: this.schedule, viewreset: this.schedule};
  }

  setData(points) {
    this.points = points.filter(hasHeatPrice);
    this.domain = heatDomain(this.points);
    this.schedule();
    return this;
  }

  schedule() {
    cancelAnimationFrame(this.frame);
    if (this.map) this.frame = requestAnimationFrame(() => this.draw());
  }

  draw() {
    if (!this.map) return;
    const size = this.map.getSize();
    if (!size.x || !size.y) return;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    this.canvas.width = Math.round(size.x*ratio);
    this.canvas.height = Math.round(size.y*ratio);
    this.canvas.style.width = `${size.x}px`;
    this.canvas.style.height = `${size.y}px`;
    L.DomUtil.setPosition(this.canvas, this.map.containerPointToLayerPoint([0,0]));
    const projected = this.points.map(point => ({...point,
      ...this.map.latLngToContainerPoint([point.latitude, point.longitude])}));
    const radius = heatRadius(this.map.getZoom(), this.map.getCenter().lat);
    const raster = heatRaster(projected, size.x, size.y, radius, this.domain);
    this.buffer.width = raster.width;
    this.buffer.height = raster.height;
    this.buffer.getContext('2d').putImageData(new ImageData(raster.pixels, raster.width, raster.height), 0, 0);
    const context = this.canvas.getContext('2d');
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    context.drawImage(this.buffer, 0, 0, this.canvas.width, this.canvas.height);
  }
}
