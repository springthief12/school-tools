const STORAGE_KEY = "message-helper-items-v2";

const messageInput = document.getElementById("messageInput");
const senderInput = document.getElementById("senderInput");
const deptInput = document.getElementById("deptInput");
const deadlineInput = document.getElementById("deadlineInput");
const priorityInput = document.getElementById("priorityInput");
const addBtn = document.getElementById("addBtn");
const clearBtn = document.getElementById("clearBtn");
const messageList = document.getElementById("messageList");
const stats = document.getElementById("stats");
const itemTemplate = document.getElementById("itemTemplate");
const filterButtons = document.querySelectorAll(".filter-btn");

let items = loadItems();
let currentFilter = "all";
render();

addBtn.addEventListener("click", () => {
  const raw = messageInput.value.trim();
  if (!raw) return;

  const item = {
    id: crypto.randomUUID(),
    raw,
    summary: summarize(raw),
    category: classify(raw),
    sender: senderInput.value.trim() || "미기재",
    dept: deptInput.value.trim() || "미기재",
    deadline: deadlineInput.value || "",
    priority: priorityInput.value,
    done: false,
    createdAt: new Date().toISOString()
  };

  items.unshift(item);
  saveItems();
  render();
  messageInput.value = "";
  messageInput.focus();
});

clearBtn.addEventListener("click", () => {
  senderInput.value = "";
  deptInput.value = "";
  deadlineInput.value = "";
  priorityInput.value = "보통";
  messageInput.value = "";
  messageInput.focus();
});

for (const button of filterButtons) {
  button.addEventListener("click", () => {
    currentFilter = button.dataset.filter;
    for (const b of filterButtons) b.classList.remove("active");
    button.classList.add("active");
    render();
  });
}

function summarize(text) {
  const compact = text.replace(/\s+/g, " ").trim();
  if (compact.length <= 90) return compact;

  const firstSentence = compact.split(/[.!?]\s/)[0];
  if (firstSentence && firstSentence.length >= 20 && firstSentence.length <= 120) {
    return firstSentence + "...";
  }

  return compact.slice(0, 90) + "...";
}

function classify(text) {
  const needKeywords = ["요청", "해줘", "필요", "확인", "수정", "마감", "제출", "부탁", "reply", "action", "todo"];
  const lowered = text.toLowerCase();
  const isNeed = needKeywords.some((k) => lowered.includes(k));
  return isNeed ? "need" : "skip";
}

function loadItems() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveItems() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

function render() {
  messageList.innerHTML = "";

  if (!items.length) {
    stats.textContent = "저장된 메시지가 없습니다.";
    return;
  }

  const visibleItems = items.filter((item) => {
    if (currentFilter === "need") return item.category === "need";
    if (currentFilter === "todo") return !item.done;
    return true;
  });

  const needCount = items.filter((i) => i.category === "need").length;
  const doneCount = items.filter((i) => i.done).length;
  stats.textContent = `전체 ${items.length}건 | 처리 필요 ${needCount}건 | 완료 ${doneCount}건 | 현재 보기 ${visibleItems.length}건`;

  if (!visibleItems.length) return;

  for (const item of visibleItems) {
    const node = itemTemplate.content.firstElementChild.cloneNode(true);

    const categoryBadge = node.querySelector(".badge.category");
    categoryBadge.textContent = item.category === "need" ? "처리 필요" : "처리 불필요";
    categoryBadge.classList.add(item.category);

    const priorityBadge = node.querySelector(".badge.priority");
    priorityBadge.textContent = `중요도 ${item.priority || "보통"}`;
    if (item.priority === "높음") priorityBadge.classList.add("priority-high");
    else if (item.priority === "낮음") priorityBadge.classList.add("priority-low");
    else priorityBadge.classList.add("priority-mid");

    node.querySelector(".sender").textContent = `보낸사람: ${item.sender || "미기재"}`;
    node.querySelector(".dept").textContent = `부서: ${item.dept || "미기재"}`;
    node.querySelector(".deadline").textContent = item.deadline ? `마감: ${item.deadline}` : "마감: 없음";

    const doneCheckbox = node.querySelector(".done-checkbox");
    doneCheckbox.checked = item.done;
    doneCheckbox.addEventListener("change", () => {
      updateItem(item.id, { done: doneCheckbox.checked });
    });

    node.querySelector(".summary").textContent = item.summary;
    node.querySelector(".raw").textContent = item.raw;

    node.querySelector(".toggle-category").addEventListener("click", () => {
      const next = item.category === "need" ? "skip" : "need";
      updateItem(item.id, { category: next });
    });

    node.querySelector(".delete").addEventListener("click", () => {
      items = items.filter((x) => x.id !== item.id);
      saveItems();
      render();
    });

    messageList.appendChild(node);
  }
}

function updateItem(id, patch) {
  items = items.map((item) => (item.id === id ? { ...item, ...patch } : item));
  saveItems();
  render();
}
