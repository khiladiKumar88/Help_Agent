import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine and no canvas, so chart components (lightweight-charts via
// fancy-canvas) cannot mount without these three shims. They are only enough to let the chart
// build and draw into nothing — assertions go against the DOM the component renders, never
// against pixels.

if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// A 2D context that answers every call: methods are no-ops, unknown properties read as 0.
function stubContext(canvas: HTMLCanvasElement): CanvasRenderingContext2D {
  const target: Record<string, unknown> = {
    canvas,
    measureText: (text: string) => ({ width: text.length * 6, actualBoundingBoxAscent: 8, actualBoundingBoxDescent: 2 }),
    createLinearGradient: () => ({ addColorStop: () => undefined }),
    createPattern: () => null,
    getImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }),
    isPointInPath: () => false,
  };
  return new Proxy(target, {
    get(obj, prop: string) {
      if (prop in obj) return obj[prop];
      return typeof prop === "string" && /^[a-z]/.test(prop) ? () => undefined : 0;
    },
    set(obj, prop: string, value) {
      obj[prop] = value;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
}

HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, kind: string) {
  return kind === "2d" ? stubContext(this) : null;
} as HTMLCanvasElement["getContext"];
