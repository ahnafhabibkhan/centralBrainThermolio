const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless:true, channel:'chrome'});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}, colorScheme:'light'});
    const page = await context.newPage();
    const errors=[];
    page.on('pageerror', e => errors.push(e.message));
    const base='http://127.0.0.1:8087';
    await page.goto(base+'/login');
    await page.locator('#token').fill('owner');
    await page.getByRole('button',{name:'Open workspace'}).click();
    await page.locator('#library-app').waitFor();
    const theme=page.getByRole('button',{name:'Dark mode',exact:true});
    await theme.click();
    assert.equal(await theme.getAttribute('aria-pressed'),'true');
    await page.reload();
    assert.equal(await theme.getAttribute('aria-pressed'),'true');
    const sibling=await context.newPage();
    await sibling.goto(base+'/library');
    await theme.focus(); await page.keyboard.press('Space');
    await sibling.waitForFunction(()=>document.documentElement.dataset.theme==='light');
    await theme.click();
    await sibling.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
    await sibling.close();
    for(const width of [320,360,390,768,1440]) {
      await page.setViewportSize({width,height:900});
      for(const mode of ['dark','light']) {
        if(await page.locator('html').getAttribute('data-theme')!==mode) await theme.click();
        for(const filter of ['ready','upload','all']) {
          await page.locator(`[data-queue-filter="${filter}"]`).click();
          assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), `Overflow at ${width}/${mode}/${filter}`);
        }
        const controls=await page.locator('.theme-toggle').boundingBox();
        assert.ok(controls.height>=44 && controls.x+controls.width<=width);
        if([390,1440].includes(width)) await page.screenshot({path:`work/upload-audit/${mode}-${width}.png`,fullPage:true});
      }
    }
    await page.setViewportSize({width:390,height:844});
    await theme.click();
    await page.locator('[data-queue-filter="upload"]').click();
    const pending=page.locator('[data-review-state="upload"]').first();
    await pending.locator('summary').click();
    await pending.locator('input[type=file]').setInputFiles({name:'wrong.xlsx',mimeType:'application/octet-stream',buffer:Buffer.from('wrong')});
    await pending.getByRole('button',{name:'Upload file',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-queue-status]').textContent.includes('checksum'));
    assert.equal(await pending.getByRole('button',{name:'Upload file',exact:true}).isEnabled(),true);
    assert.equal(await pending.getByRole('button',{name:'Approve',exact:true}).count(),0);
    await pending.locator('input[type=file]').setInputFiles({name:'Financial forecast.xlsx',mimeType:'application/octet-stream',buffer:Buffer.from('xlsx original'.repeat(15000))});
    for(const fault of ['network','gateway','rate','session']) {
      await page.route('**/library/suggestions/*/original', route => {
        if(fault==='network') return route.abort();
        if(fault==='gateway') return route.fulfill({status:502,contentType:'text/html',body:'<h1>Gateway error</h1>'});
        if(fault==='rate') return route.fulfill({status:429,headers:{'Retry-After':'2'},body:'Slow down'});
        return route.fulfill({status:401,contentType:'application/json',body:'{}'});
      });
      await pending.getByRole('button',{name:'Upload file',exact:true}).click();
      await page.waitForFunction(()=>!document.querySelector('[data-queue-upload] button').disabled);
      const text=await page.locator('[data-queue-status]').innerText();
      assert.match(text,/refresh|Refresh|Sign in/);
      assert.doesNotMatch(text,/Unexpected token|JSON/);
      await page.unroute('**/library/suggestions/*/original');
    }
    await pending.getByRole('button',{name:'Upload file',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-queue-status]').textContent.startsWith('Saved.'));
    assert.equal(await page.locator('[data-review-state="upload"]').count(),0);
    const ready=page.locator('[data-review-state="ready"]').filter({hasText:'Financial forecast.xlsx'});
    assert.equal(await ready.getByRole('button',{name:'Approve',exact:true}).count(),1);
    // Simulate a lost response after a successful approval request. The refresh must recover the actual state.
    let lost=false;
    await page.route('**/library/approve-all', async route => {
      if(!lost) { lost=true; await route.fetch(); return route.abort(); }
      await route.continue();
    });
    const before=await page.locator('[data-review-state="ready"]').count();
    await page.locator('.bulk-approval button').click();
    await page.waitForFunction(()=>document.querySelector('[data-queue-status]').textContent.includes('approvals confirmed'));
    assert.equal(await page.locator('[data-review-state="ready"]').count(),before-3);
    await page.unroute('**/library/approve-all');
    await page.locator('.bulk-approval button').click();
    await page.waitForFunction(()=>document.querySelector('[data-queue-status]').textContent.includes('items approved.'));
    assert.equal(await page.locator('[data-review-state="ready"]').count(),0);
    assert.ok(await page.locator('#approvals').isVisible());
    await page.locator('a[data-folder]').filter({hasText:'Projects'}).first().click();
    await page.locator('.folder-settings').click();
    await page.locator('dialog[open]').waitFor();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:'work/upload-audit/dark-dialog-mobile.png',fullPage:true});
    const denied=await browser.newContext({colorScheme:'dark'});
    await denied.addInitScript(()=>{Object.defineProperty(window,'localStorage',{get(){throw new Error('Storage disabled');}});});
    const dp=await denied.newPage();
    await dp.goto(base+'/login');
    assert.equal(await dp.locator('html').getAttribute('data-theme'),'dark');
    await dp.getByRole('button',{name:'Dark mode',exact:true}).click();
    assert.equal(await dp.locator('html').getAttribute('data-theme'),'light');
    await denied.close();
    assert.deepEqual(errors,[]);
    console.log('PASS: theme persistence, keyboard, cross-tab, blocked storage, five widths in both themes, all queue filters, incorrect originals, interrupted network, gateway/rate/session errors, retry, upload readiness, ambiguous approval recovery, bulk approval, mobile dialog, and no browser errors.');
  } finally {await browser.close();}
})();
