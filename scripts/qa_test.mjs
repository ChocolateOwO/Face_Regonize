export default async function run(page, ui) {
  const results = {};

  // Should redirect to login
  await page.waitForSelector('input[placeholder="admin"]', { timeout: 10000 });
  results.loginPageShown = true;

  await page.fill('input[placeholder="admin"]', 'admin');
  await page.fill('input[placeholder="••••••••"]', 'admin123');
  await page.click('button[type="submit"]');

  await page.waitForSelector('text=Dashboard', { timeout: 10000 });
  await page.waitForTimeout(800);
  results.dashboardLoaded = true;
  results.dashboardText = await page.textContent('body');

  // Go to People page
  await page.click('text=People');
  await page.waitForTimeout(800);
  results.peopleText = await page.textContent('body');

  // Go to Recognition page
  await page.click('text=Face Recognition');
  await page.waitForTimeout(500);
  results.recognitionText = await page.textContent('body');

  // Go to Reports page
  await page.click('text=Reports');
  await page.waitForTimeout(500);
  results.reportsText = await page.textContent('body');

  // Go to Settings
  await page.click('text=Settings');
  await page.waitForTimeout(500);
  results.settingsText = await page.textContent('body');

  return results;
}
