const form = document.querySelector("#chatForm");
const input = document.querySelector("#messageInput");
const messages = document.querySelector("#messages");
const sendButton = document.querySelector("#sendButton");

function addMessage(text, role) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  article.appendChild(paragraph);
  messages.appendChild(article);
  messages.scrollTop = messages.scrollHeight;
}

async function sendMessage(message) {
  sendButton.disabled = true;
  addMessage(message, "user");
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The assistant is unavailable.");
    addMessage(data.reply, data.blocked ? "error" : "bot");
  } catch (error) {
    addMessage(error.message, "error");
  } finally {
    sendButton.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (message) {
    input.value = "";
    sendMessage(message);
  }
});

const adminPanel = document.querySelector("#adminPanel");
const adminRequests = document.querySelector("#adminRequests");
let adminKey = "";

async function loadAdminRequests() {
  const response = await fetch("/api/admin/requests", {
    headers: adminKey ? { "X-Admin-Key": adminKey } : {}
  });
  const data = await response.json();
  adminRequests.replaceChildren();
  if (!data.requests.length) {
    const empty = document.createElement("p");
    empty.textContent = "No pending reservation requests.";
    adminRequests.appendChild(empty);
    return;
  }
  data.requests.forEach((item) => {
    const row = document.createElement("div");
    row.className = "admin-request";
    const details = document.createElement("div");
    details.textContent = `${item.reference} - ${item.customer_name} - ${item.vehicle_type} - ${item.reservation_date} ${item.arrival_time}`;
    const actions = document.createElement("div");
    actions.className = "admin-actions";
    const approve = document.createElement("button");
    approve.textContent = "Approve";
    approve.addEventListener("click", () => respondAdmin(item.reference, "approve"));
    const refuse = document.createElement("button");
    refuse.textContent = "Refuse";
    refuse.className = "refuse";
    refuse.addEventListener("click", () => respondAdmin(item.reference, "refuse"));
    actions.append(approve, refuse);
    row.append(details, actions);
    adminRequests.appendChild(row);
  });
}

async function respondAdmin(reference, decision) {
  const response = await fetch(`/api/admin/requests/${encodeURIComponent(reference)}/${decision}`, {
    method: "POST",
    headers: adminKey ? { "X-Admin-Key": adminKey } : {}
  });
  if (response.ok) {
    await loadAdminRequests();
  } else {
    const data = await response.json();
    addMessage(data.error || "Unable to update the request.", "error");
  }
}

document.querySelector("#adminButton").addEventListener("click", async () => {
  if (!adminKey) {
    const key = window.prompt("Administrator key (leave empty only when running in development mode):");
    if (key !== null) adminKey = key.trim();
  }
  adminPanel.hidden = false;
  await loadAdminRequests();
});
document.querySelector("#closeAdminButton").addEventListener("click", () => {
  adminPanel.hidden = true;
});

document.querySelector("#resetButton").addEventListener("click", async () => {
  await fetch("/api/reset", { method: "POST" });
  messages.innerHTML = "";
  addMessage("Conversation reset. How can I help?", "bot");
});

document.querySelectorAll("[data-message]").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.message));
});
