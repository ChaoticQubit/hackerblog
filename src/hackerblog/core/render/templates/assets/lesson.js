(() => {
  "use strict";

  const source = document.getElementById("lesson-data");
  if (!source) {
    return;
  }
  const data = JSON.parse(source.textContent || "{}");
  const results = {};
  document.documentElement.classList.add("js");

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  };

  const svgIcon = (name) => {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "icon mark");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(ns, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  };

  const button = (label, cls) => {
    const node = el("button", cls || "btn", label);
    node.type = "button";
    return node;
  };

  const store = {
    load() {
      try {
        return JSON.parse(window.localStorage.getItem(data.storage_key) || "{}");
      } catch {
        return {};
      }
    },
    save(state) {
      try {
        window.localStorage.setItem(data.storage_key, JSON.stringify(state));
      } catch {
        return false;
      }
      return true;
    },
  };

  const normalise = (text) => String(text).toLowerCase().replace(/\s+/g, " ").trim();

  const showFeedback = (box, state, title, detail) => {
    box.replaceChildren();
    box.dataset.state = state;
    const marks = { ok: "check", no: "cross", hint: "idea" };
    box.append(svgIcon(marks[state]), el("strong", null, title));
    if (detail) {
      box.append(document.createTextNode(" " + detail));
    }
  };

  const updateScore = () => {
    const target = document.getElementById("score");
    if (!target || data.final.length === 0) {
      return;
    }
    const answered = data.final.filter((key) => key in results);
    const right = answered.filter((key) => results[key]).length;
    if (answered.length < data.final.length) {
      target.textContent = `${answered.length} of ${data.final.length} answered, ${right} right so far.`;
      return;
    }
    target.textContent = `Your score: ${right} of ${data.final.length}.`;
    const state = store.load();
    state.last_score = { right, total: data.final.length };
    store.save(state);
  };

  const record = (key, ok) => {
    results[key] = ok;
    updateScore();
  };

  const mcq = (item, key, root) => {
    const set = el("fieldset");
    set.append(el("legend", "q", item.question));
    item.options.forEach((option, index) => {
      const label = el("label", "choice");
      const input = el("input");
      input.type = "radio";
      input.name = `${key}-choice`;
      input.value = String(index);
      label.append(input, el("span", null, option));
      set.append(label);
    });
    root.append(set);
    return () => {
      const picked = set.querySelector("input:checked");
      if (!picked) {
        return null;
      }
      return {
        ok: Number(picked.value) === item.correct_index,
        answer: item.options[item.correct_index],
      };
    };
  };

  const shortAnswer = (item, key, root) => {
    const label = el("label", "field q", item.question);
    label.htmlFor = `${key}-input`;
    const input = el("input");
    input.type = "text";
    input.id = `${key}-input`;
    input.autocomplete = "off";
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        root.querySelector(".check")?.click();
      }
    });
    root.append(label, input);
    return () => {
      if (!input.value.trim()) {
        return null;
      }
      const given = normalise(input.value);
      return {
        ok: item.accepted_answers.some((answer) => normalise(answer) === given),
        answer: item.accepted_answers[0],
      };
    };
  };

  const moveItem = (list, row, delta, announce) => {
    const rows = Array.from(list.children);
    const index = rows.indexOf(row);
    const target = index + delta;
    if (target < 0 || target >= rows.length) {
      return;
    }
    if (delta < 0) {
      list.insertBefore(row, rows[target]);
    } else {
      list.insertBefore(row, rows[target].nextSibling);
    }
    Array.from(list.children).forEach((child, position) => {
      child.querySelector(".order-pos").textContent = String(position + 1);
    });
    row.focus();
    announce(`${row.dataset.label} moved to position ${target + 1} of ${rows.length}.`);
  };

  const orderRow = (list, text, index, announce) => {
    const row = el("li", "order-item");
    row.tabIndex = 0;
    row.dataset.index = String(index);
    row.dataset.label = text;
    row.setAttribute("aria-label", `${text}. Use the arrow keys or the buttons to move it.`);
    const up = button("Up", "btn btn-quiet btn-small");
    up.setAttribute("aria-label", `Move ${text} up`);
    const down = button("Down", "btn btn-quiet btn-small");
    down.setAttribute("aria-label", `Move ${text} down`);
    up.addEventListener("click", () => moveItem(list, row, -1, announce));
    down.addEventListener("click", () => moveItem(list, row, 1, announce));
    row.addEventListener("keydown", (event) => {
      if (event.target !== row) {
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        moveItem(list, row, event.key === "ArrowUp" ? -1 : 1, announce);
      }
    });
    row.append(el("span", "order-pos"), el("span", "order-text", text), up, down);
    return row;
  };

  const ordering = (item, key, root) => {
    root.append(el("p", "q", item.prompt));
    const status = el("p", "visually-hidden");
    status.setAttribute("aria-live", "polite");
    const announce = (text) => {
      status.textContent = text;
    };
    const list = el("ol", "order-list");
    item.shuffle.forEach((index, position) => {
      const row = orderRow(list, item.steps_in_order[index], index, announce);
      row.querySelector(".order-pos").textContent = String(position + 1);
      list.append(row);
    });
    root.append(list, status);
    return () => {
      const order = Array.from(list.children).map((row) => Number(row.dataset.index));
      return {
        ok: order.every((value, position) => value === position),
        answer: item.steps_in_order.join(", then "),
      };
    };
  };

  const spotTheBug = (item, key, root) => {
    const set = el("fieldset");
    set.append(el("legend", "q", item.prompt));
    item.lines.forEach((line, index) => {
      const label = el("label", "choice");
      const input = el("input");
      input.type = "radio";
      input.name = `${key}-line`;
      input.value = String(index);
      label.append(input, el("span", "line-no", index + 1), el("code", "choice-code", line));
      set.append(label);
    });
    root.append(set);
    return () => {
      const picked = set.querySelector("input:checked");
      if (!picked) {
        return null;
      }
      return {
        ok: Number(picked.value) === item.bug_line,
        answer: `line ${item.bug_line + 1}: ${item.lines[item.bug_line]}`,
      };
    };
  };

  const matchPairs = (item, key, root) => {
    root.append(el("p", "q", item.prompt));
    const selects = item.pairs.map(([left], index) => {
      const row = el("div", "pair");
      const label = el("label", "field", left);
      const select = el("select");
      select.id = `${key}-pair-${index}`;
      label.htmlFor = select.id;
      select.append(el("option", null, "Choose a match"));
      select.options[0].value = "";
      item.shuffle.forEach((choice) => {
        const option = el("option", null, item.pairs[choice][1]);
        option.value = String(choice);
        select.append(option);
      });
      row.append(label, select);
      root.append(row);
      return select;
    });
    return () => {
      if (selects.some((select) => select.value === "")) {
        return null;
      }
      return {
        ok: selects.every((select, index) => item.pairs[Number(select.value)][1] === item.pairs[index][1]),
        answer: item.pairs.map(([left, right]) => `${left}: ${right}`).join("; "),
      };
    };
  };

  const builders = {
    mcq,
    short_answer: shortAnswer,
    ordering,
    spot_the_bug: spotTheBug,
    match_pairs: matchPairs,
  };

  const mountItem = (root) => {
    const key = root.dataset.item;
    const item = data.items[key];
    const build = item && builders[item.type];
    if (!build) {
      return;
    }
    const read = build(item, key, root);
    const row = el("div", "row");
    const check = button("Check answer", "btn check");
    const feedback = el("p", "feedback");
    feedback.setAttribute("aria-live", "polite");
    check.addEventListener("click", () => {
      const result = read();
      if (result === null) {
        showFeedback(feedback, "hint", "Answer first.", "Choose or type an answer, then check it.");
        return;
      }
      if (result.ok) {
        showFeedback(feedback, "ok", "Correct.", item.explanation);
      } else {
        showFeedback(feedback, "no", "Not quite.", `The answer is ${result.answer}. ${item.explanation}`);
      }
      record(key, result.ok);
    });
    row.append(check);
    root.append(row, feedback);
  };

  const mountExplorable = () => {
    const root = document.getElementById("explorable");
    const spec = data.explorable;
    if (!root || !spec) {
      return;
    }
    const label = el("label", "field", `${spec.label} (${spec.unit})`);
    const input = el("input");
    input.type = "range";
    input.id = "explore-range";
    label.htmlFor = input.id;
    input.min = String(spec.min);
    input.max = String(spec.max);
    input.step = String(spec.step);
    input.value = String(spec.points[0].at);
    const value = el("output", "explore-value");
    value.htmlFor = input.id;
    const outcome = el("p", "explore-outcome");
    outcome.setAttribute("aria-live", "polite");
    const update = () => {
      const current = Number(input.value);
      const nearest = spec.points.reduce((best, point) =>
        Math.abs(point.at - current) < Math.abs(best.at - current) ? point : best);
      value.textContent = `${current} ${spec.unit}`;
      outcome.textContent = nearest.outcome;
    };
    input.addEventListener("input", update);
    root.append(label, input, value, outcome);
    update();
  };

  const mountFlashcards = () => {
    const root = document.getElementById("flashcards");
    const cards = data.flashcards;
    if (!root || cards.length === 0) {
      return;
    }
    let index = 0;
    const face = button("", "cards-face");
    const count = el("p", "cards-count");
    count.setAttribute("aria-live", "polite");
    const show = (flipped) => {
      const card = cards[index];
      face.setAttribute("aria-pressed", flipped ? "true" : "false");
      face.textContent = flipped ? card.definition : card.term;
      face.setAttribute("aria-label", flipped ? `${card.term}: ${card.definition}` : `${card.term}. Press to show the definition.`);
      count.textContent = `Card ${index + 1} of ${cards.length}${flipped ? ", definition shown" : ""}`;
    };
    face.addEventListener("click", () => show(face.getAttribute("aria-pressed") !== "true"));
    const previous = button("Previous card", "btn btn-quiet btn-small");
    const next = button("Next card", "btn btn-quiet btn-small");
    previous.addEventListener("click", () => {
      index = (index - 1 + cards.length) % cards.length;
      show(false);
    });
    next.addEventListener("click", () => {
      index = (index + 1) % cards.length;
      show(false);
    });
    const row = el("div", "row");
    row.append(previous, next, count);
    root.append(face, row);
    show(false);
  };

  const shellQuote = (text) => `'${text.replace(/'/g, "'\\''")}'`;

  const mountAsk = () => {
    const input = document.getElementById("ask-question");
    const command = document.getElementById("ask-command");
    if (!input || !command) {
      return;
    }
    const update = () => {
      const question = input.value.replace(/[\u0000-\u001f\u007f]/g, " ").trim() || "What should I remember from this post?";
      command.textContent = `hackerblog ask --post ${data.post_id} ${shellQuote(question)}`;
    };
    input.addEventListener("input", update);
    update();
  };

  const copyStatus = (text) => {
    const status = document.getElementById("copy-status");
    if (status) {
      status.textContent = text;
    }
  };

  const mountCopy = () => {
    document.querySelectorAll("[data-copy]").forEach((trigger) => {
      trigger.addEventListener("click", () => {
        const target = document.getElementById(trigger.dataset.copy);
        if (!target) {
          return;
        }
        const text = target.textContent || "";
        const fallback = () => {
          window.getSelection()?.selectAllChildren(target);
          copyStatus("Copying is blocked here. The text is selected: press Ctrl+C or Cmd+C.");
        };
        if (!navigator.clipboard) {
          fallback();
          return;
        }
        navigator.clipboard.writeText(text).then(() => copyStatus("Copied."), fallback);
      });
    });
  };

  const mountScore = () => {
    const reset = document.getElementById("reset");
    const target = document.getElementById("score");
    const last = store.load().last_score;
    if (target && last) {
      target.textContent = `Last time you scored ${last.right} of ${last.total}. Answer again to update it.`;
    }
    if (reset) {
      reset.addEventListener("click", () => {
        Object.keys(results).forEach((key) => delete results[key]);
        store.save({});
        document.querySelectorAll(".widget[data-item]").forEach((root) => {
          const kind = root.querySelector(".widget-kind");
          root.replaceChildren(kind);
          mountItem(root);
        });
        if (target) {
          target.textContent = "Answer every question to see your score.";
        }
      });
    }
  };

  document.querySelectorAll(".widget[data-item]").forEach(mountItem);
  mountExplorable();
  mountFlashcards();
  mountAsk();
  mountCopy();
  mountScore();
})();
