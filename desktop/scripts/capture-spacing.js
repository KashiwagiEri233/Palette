const __name = (value) => value;
(function captureSpacingSnapshot() {
  const maxElements = 1500;
  const candidates = Array.from(document.querySelectorAll("body *"));
  const hiddenByAncestor = /* @__PURE__ */ __name((element) => {
    const visible2 = element.checkVisibility?.({
      opacityProperty: true,
      visibilityProperty: true,
      contentVisibilityAuto: true
    });
    if (visible2 !== void 0) return !visible2;
    for (let node3 = element; node3; node3 = node3.parentElement) {
      if (Number(getComputedStyle(node3).opacity) === 0) return true;
    }
    return false;
  }, "hiddenByAncestor");
  const visible = candidates.filter((element) => {
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    if (hiddenByAncestor(element)) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }).slice(0, maxElements);
  const ids = /* @__PURE__ */ new Map();
  const selectorFor = /* @__PURE__ */ __name((element, index3) => {
    const explicit = element.getAttribute("data-bd-inspect-id");
    if (explicit) return explicit;
    if (element.id) return `#${element.id}`;
    const tag2 = element.tagName.toLowerCase();
    const parent = element.parentElement;
    if (!parent) return `${tag2}:${index3 + 1}`;
    const siblings = Array.from(parent.children).filter((child) => child.tagName === element.tagName);
    return `${tag2}:nth-of-type(${siblings.indexOf(element) + 1})@${index3 + 1}`;
  }, "selectorFor");
  visible.forEach((element, index3) => ids.set(element, selectorFor(element, index3)));
  const number4 = /* @__PURE__ */ __name((value) => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }, "number");
  const edges = /* @__PURE__ */ __name((style, prefix) => ({
    top: number4(style[`${prefix}Top`]),
    right: number4(style[`${prefix}Right`]),
    bottom: number4(style[`${prefix}Bottom`]),
    left: number4(style[`${prefix}Left`])
  }), "edges");
  const resolvedLength = /* @__PURE__ */ __name((value, size) => {
    const trimmed = value.trim();
    if (!trimmed.startsWith("calc(") || !trimmed.endsWith(")")) {
      return trimmed.endsWith("%") ? number4(trimmed) * size / 100 : number4(trimmed);
    }
    const expression = trimmed.slice(5, -1).trim();
    const term = /\s*([+-]?)\s*((?:\d+(?:\.\d*)?|\.\d+))(px|%)/gy;
    let total = 0;
    let cursor = 0;
    while (cursor < expression.length) {
      term.lastIndex = cursor;
      const match2 = term.exec(expression);
      if (!match2) return 0;
      const sign5 = match2[1] === "-" ? -1 : 1;
      const amount = Number(match2[2]);
      total += sign5 * (match2[3] === "%" ? amount * size / 100 : amount);
      cursor = term.lastIndex;
    }
    return total;
  }, "resolvedLength");
  const radiusComponents = /* @__PURE__ */ __name((value) => {
    const parts = [];
    let depth = 0;
    let current = "";
    for (const character of value.trim()) {
      if (/\s/.test(character) && depth === 0) {
        if (current) parts.push(current);
        current = "";
        continue;
      }
      if (character === "(") depth += 1;
      if (character === ")") depth -= 1;
      current += character;
    }
    if (current) parts.push(current);
    return parts;
  }, "radiusComponents");
  const cornerRadius = /* @__PURE__ */ __name((value, rect) => {
    const [horizontal = "0", vertical = horizontal] = radiusComponents(value);
    return {
      horizontal: resolvedLength(horizontal, rect.width),
      vertical: resolvedLength(vertical, rect.height)
    };
  }, "cornerRadius");
  const attribute2 = /* @__PURE__ */ __name((element, name18) => element.getAttribute(name18) || void 0, "attribute");
  const alignmentEdge = /* @__PURE__ */ __name((value) => value === "left" || value === "right" || value === "top" || value === "bottom" || value === "center-x" || value === "center-y" ? value : void 0, "alignmentEdge");
  const symmetry = /* @__PURE__ */ __name((value) => value === "horizontal" || value === "vertical" || value === "both" ? value : void 0, "symmetry");
  const compareSize = /* @__PURE__ */ __name((value) => value === "width" || value === "height" || value === "both" ? value : void 0, "compareSize");
  const interactiveRoles = /* @__PURE__ */ new Set(["button", "checkbox", "combobox", "link", "menuitem", "radio", "slider", "switch", "tab"]);
  const interactiveTags = /* @__PURE__ */ new Set(["a", "button", "input", "select", "summary", "textarea"]);
  const lineBoxes = /* @__PURE__ */ __name((element) => {
    const tops = [];
    let sawText = false;
    const walk = /* @__PURE__ */ __name((node3) => {
      for (const child of Array.from(node3.childNodes)) {
        if (child.nodeType === 3) {
          if (!(child.textContent || "").trim()) continue;
          sawText = true;
          const range2 = document.createRange();
          range2.selectNodeContents(child);
          for (const box of Array.from(range2.getClientRects())) {
            if (box.width <= 0 || box.height <= 0) continue;
            if (!tops.some((top) => Math.abs(top - box.top) <= 1)) tops.push(box.top);
          }
          continue;
        }
        if (child.nodeType !== 1) continue;
        const childElement = child;
        const childStyle = getComputedStyle(childElement);
        if (childStyle.position === "absolute" || childStyle.position === "fixed") continue;
        if (childStyle.display === "none" || childStyle.visibility === "hidden") continue;
        if (childStyle.display !== "inline" && childStyle.display !== "contents") continue;
        walk(childElement);
      }
    }, "walk");
    walk(element);
    return sawText && tops.length ? tops.length : void 0;
  }, "lineBoxes");
  const elements = visible.map((element) => {
    const html3 = element;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    const tag2 = element.tagName.toLowerCase();
    const role = attribute2(element, "role");
    const parentId = element.parentElement ? ids.get(element.parentElement) : void 0;
    const tabIndex = html3.tabIndex;
    const interactive = interactiveTags.has(tag2) || interactiveRoles.has(role || "") || tabIndex >= 0;
    const explicitLabel = attribute2(element, "data-bd-inspect-label") || attribute2(element, "aria-label");
    const text5 = (explicitLabel || (interactive ? element.textContent : "") || "").replace(/\s+/g, " ").trim().slice(0, 80);
    const id = ids.get(element);
    return {
      id,
      selector: id,
      tag: tag2,
      role,
      label: text5 || void 0,
      parentId,
      groupId: attribute2(element, "data-bd-group"),
      alignmentGroup: attribute2(element, "data-bd-align-group"),
      alignmentEdge: alignmentEdge(attribute2(element, "data-bd-align-edge")),
      expectedSymmetry: symmetry(attribute2(element, "data-bd-symmetry")),
      compareSize: compareSize(attribute2(element, "data-bd-compare-size")),
      interactive,
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      style: {
        display: style.display,
        position: style.position,
        flexDirection: style.flexDirection,
        flexWrap: style.flexWrap,
        alignItems: style.alignItems,
        justifyContent: style.justifyContent,
        rowGap: number4(style.rowGap),
        columnGap: number4(style.columnGap),
        padding: edges(style, "padding"),
        margin: edges(style, "margin"),
        overflowX: style.overflowX,
        overflowY: style.overflowY,
        borderRadius: {
          topLeft: cornerRadius(style.borderTopLeftRadius, rect),
          topRight: cornerRadius(style.borderTopRightRadius, rect),
          bottomRight: cornerRadius(style.borderBottomRightRadius, rect),
          bottomLeft: cornerRadius(style.borderBottomLeftRadius, rect)
        },
        backgroundColor: style.backgroundColor,
        backgroundImage: style.backgroundImage,
        boxShadow: style.boxShadow
      },
      lineCount: lineBoxes(element),
      clientWidth: html3.clientWidth,
      clientHeight: html3.clientHeight,
      scrollWidth: html3.scrollWidth,
      scrollHeight: html3.scrollHeight
    };
  });
  return {
    name: `viewport-${window.innerWidth}`,
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio
    },
    elements
  };
})()
