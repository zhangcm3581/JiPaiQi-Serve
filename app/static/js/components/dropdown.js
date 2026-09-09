import { escapeHtml as esc } from "./cards.js";
let count = 0;
export class Dropdown {
  constructor(element, onChange, options = []) {
    this.element = element;
    this.onChange = onChange;
    this.id = "dropdown-" + ++count;
    element.classList.add("tenant-picker");
    element.innerHTML = `<button type="button" class="picker-trigger" aria-haspopup="listbox" aria-expanded="false" aria-controls="${this.id}"><span></span><svg><use href="#arrow"/></svg></button><div class="picker-menu" id="${this.id}" role="listbox" hidden></div>`;
    this.button = element.querySelector("button");
    this.menu = element.querySelector(".picker-menu");
    this.button.onclick = () => (this.menu.hidden ? this.open() : this.close());
    this.menu.onclick = (e) => {
      const b = e.target.closest("[data-value]");
      if (b) {
        this.value = b.dataset.value;
        this.update(this.options, this.value);
        this.close();
        this.button.focus();
        this.onChange(this.value);
      }
    };
    document.addEventListener("click", (e) => {
      if (!element.contains(e.target)) this.close();
    });
    element.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        this.close();
        this.button.focus();
      }
      if (e.key === "Tab") this.close();
      if (
        !this.menu.hidden &&
        ["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)
      ) {
        e.preventDefault();
        let list = [...this.menu.querySelectorAll("button")],
          i = list.indexOf(document.activeElement);
        if (list.length) {
          let j =
            e.key === "Home"
              ? 0
              : e.key === "End"
                ? list.length - 1
                : (i + (e.key === "ArrowDown" ? 1 : -1) + list.length) %
                  list.length;
          list[j].focus();
        }
      }
    });
    this.update(options, "");
  }
  open() {
    this.menu.hidden = false;
    this.button.setAttribute("aria-expanded", "true");
    this.menu.querySelector("[aria-selected=true]")?.focus();
  }
  close() {
    this.menu.hidden = true;
    this.button.setAttribute("aria-expanded", "false");
  }
  update(options, value) {
    const signature = JSON.stringify([options, value]);
    if (signature === this.signature) return;
    this.signature = signature;
    this.options = options;
    this.value = value;
    this.button.querySelector("span").textContent =
      options.find((o) => o.value === value)?.label || "选择租户";
    this.button.disabled = !options.length;
    this.menu.innerHTML = options
      .map(
        (o) =>
          `<button class="picker-option" role="option" aria-selected="${o.value === value}" data-value="${esc(o.value)}"><span><strong>${esc(o.label)}</strong>${o.note ? `<small>${esc(o.note)}</small>` : ""}</span><svg><use href="#check"/></svg></button>`,
      )
      .join("");
  }
}
