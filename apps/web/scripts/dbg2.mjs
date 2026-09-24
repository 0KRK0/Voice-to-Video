import { chromium } from "playwright";
const b = await chromium.launch();
const p = await b.newPage();
p.on("response", r => { if (r.status() >= 400) console.log("HTTP", r.status(), r.url()); });
await p.goto(process.argv[2], { waitUntil: "networkidle" });
await p.waitForTimeout(2000);
await b.close();
