import { expect, test } from "@playwright/test";

const LINK = "/?token=e2e-token"; // what `serve` prints
const EXPORTS = "/tmp/resink-e2e/exports";

test("a folder of exports becomes tables you can inspect and query, in both themes", async ({ page }) => {
  // the page needs the token, and the token is removed from the address bar once it is a cookie
  const nav = page.getByRole("navigation", { name: "Main" });
  expect((await page.request.get("/")).status()).toBe(401);
  await page.goto(LINK);
  await expect(page.getByRole("heading", { name: "Tables" })).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText("No tables yet")).toBeVisible();

  // L2.1 browse folders: add the folder, see every file proposed, nothing imported yet
  await nav.getByRole("link", { name: "Folders" }).click();
  await page.getByLabel("Folder on this computer").fill(EXPORTS);
  await page.getByRole("button", { name: "Propose" }).click();
  await expect(page.getByRole("status")).toContainText("50 files proposed");
  await expect(page.getByRole("row")).toHaveCount(51); // header + 50 files
  await nav.getByRole("link", { name: "Tables" }).click();
  await expect(page.getByText("No tables yet")).toBeVisible();

  // L2.2 change an inferred column type, then import
  await nav.getByRole("link", { name: "Folders" }).click();
  const row = page.getByRole("row", { name: /thousands_a\.csv/ });
  await row.getByRole("button", { name: "Review" }).click();
  const review = page.getByRole("region", { name: "Review thousands_a.csv" });
  await expect(review.getByLabel("Type of amount")).toHaveValue("double");
  await review.getByLabel("Type of amount").selectOption("string");
  await expect(review.getByText("inferred double")).toBeVisible();
  await review.getByRole("button", { name: "Import this file" }).click();
  await expect(page.getByRole("status")).toContainText("1 imported, 0 failed");

  // L2.1 schema, sample rows, snapshot history
  await nav.getByRole("link", { name: "Tables" }).click();
  await page.getByRole("link", { name: /exports\.thousands_a/ }).click();
  await expect(page.getByRole("cell", { name: "string" }).first()).toBeVisible();
  await expect(page.getByText("ready to push")).toBeVisible();
  await page.getByRole("tab", { name: "Sample rows" }).click();
  await expect(page.getByRole("columnheader", { name: "amount" })).toBeVisible();
  await expect(page.getByRole("row")).toHaveCount(9); // header + 8 rows
  await page.getByRole("tab", { name: "Snapshot history" }).click();
  await expect(page.getByRole("cell", { name: "append" })).toBeVisible();

  // L2.3 SQL
  await nav.getByRole("link", { name: "SQL" }).click();
  await page.getByLabel("SQL query").fill("select count(*) as n from exports.thousands_a");
  await page.getByLabel("SQL query").press("Control+Enter");
  await expect(page.getByRole("cell", { name: "8", exact: true })).toBeVisible();
  await page.getByLabel("SQL query").fill("drop table exports.thousands_a");
  await page.getByRole("button", { name: "Run" }).click();
  await expect(page.getByRole("alert")).toContainText("read-only");

  // L2.4 both themes: the page colours really change
  const bg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.emulateMedia({ colorScheme: "light" });
  const light = await bg();
  await page.emulateMedia({ colorScheme: "dark" });
  const dark = await bg();
  expect(light).not.toBe(dark); // the system theme follows the OS
  await page.getByRole("button", { name: /Theme/ }).click(); // -> light, forced
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await bg()).toBe(light);
  await page.getByRole("button", { name: /Theme/ }).click(); // -> dark, forced
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await bg()).toBe(dark);
  await page.reload(); // the choice survives a reload
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});
