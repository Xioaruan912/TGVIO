import "../../src/style.css";
import { buildCoverTile } from "../../src/components/cover-tile";
import { buildLogin } from "../../src/ui";
const app = document.querySelector("#app")!;
if (new URLSearchParams(location.search).has("login")) {
  app.append(buildLogin(async () => {
    await new Promise(resolve => setTimeout(resolve, 600));
    throw { code: "unauthorized" };
  }));
} else {
  const page = document.createElement("section");
  page.className = "long-page library-page";
  const heading = document.createElement("h1");
  heading.textContent = "隔离视频帧 · 封面状态验收";
  heading.style.cssText = "font-size:20px;padding:16px;margin:0";
  const list = document.createElement("div");
  list.className = "library-list cover-grid";
  page.append(heading, list); app.append(page);
  for (let index = 0; index < 5; index++) {
    const tile = buildCoverTile({
      media: { id: String(index).repeat(64), duration: index === 1 ? 0 : 62, category: "short",
        favorite: index === 0, coverUrl: index === 1 ? null : index === 2 ? "/__acceptance__/cover/broken" : "/__acceptance__/cover/short-" + index % 3 },
      title: index === 0 ? "这是一个很长的隔离验收标题，检查两行截断和信息安全间距" : "视频 #" + String(index).repeat(8),
      showCategory: true, onPlay() {}, onPreview() {}, onSelect() {},
    });
    list.append(tile.root);
  }
}
