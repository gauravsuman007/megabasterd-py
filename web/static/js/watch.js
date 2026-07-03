document.getElementById("watch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const link = e.target.link.value;

    const resp = await fetch(`/api/stream/info?link=${encodeURIComponent(link)}`);
    if (!resp.ok) { alert(await resp.text()); return; }
    const info = await resp.json();

    document.getElementById("player-title").textContent = info.name;
    const player = document.getElementById("player");
    player.src = `/stream?link=${encodeURIComponent(link)}`;
    document.getElementById("player-card").style.display = "block";
    player.play().catch(() => {});
});
