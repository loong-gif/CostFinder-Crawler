const source = "https://mariposaskincare.com/collections/specials-gifts-with-purchase";
const task = await taskSpace(6);
const page = task.page("p1");
try { await page.goto(source, { timeout: 15000, waitUntil: "commit" }); } catch (error) { console.log(`navigation: ${String(error).slice(0, 500)}`); }
await page.waitForTimeout(3000);
const body = String(await page.evaluate(() => document.body?.innerText || ""));
const snapshot = String(await page.snapshot({ scope: "full_page" }));
console.log(JSON.stringify({ url: await page.url(), title: await page.title(), body, snapshot }, null, 2));
