# 示例：将后续操作包裹在独立 try 块中，失败仅记录不抛出异常
try:
    # 4. 批量商品
    await click_and_wait_element(
        page.locator("tbody tr").first.locator("a[href$='/items']"),
        page.locator("a[href*='/items/new'], a:has-text('導入商品')").first
    )
    await click_and_wait_element(
        page.locator("a[href*='/items/new'], a:has-text('導入商品')").first,
        page.locator("#count_of_items, input[name='count_of_items']")
    )
    await page.locator("#count_of_items, input[name='count_of_items']").fill("60")
    await page.locator("input[name='commit'], input[value='送出']").click()
    await page.wait_for_load_state("domcontentloaded")
    
    # 5. 移除非银行卡占位符
    if info_type != "bank":
        await search_account(final_account)
        await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
        remove_btn = page.locator("a.remove_fields.dynamic, a:has-text('移除')").first
        if await remove_btn.is_visible():
            await remove_btn.click()
        await page.locator("input[name='commit'], input[value='送出']").click()
        await page.wait_for_load_state("domcontentloaded")

    # 6. 出货订单
    await search_account(final_account)
    await click_and_wait_element(
        page.locator("tbody tr").first.locator("a[href$='/deposits']"),
        page.locator("a[href$='/deposits/new'], a:has-text('輸入出貨訂單')").first
    )
    await click_and_wait_element(
        page.locator("a[href$='/deposits/new'], a:has-text('輸入出貨訂單')").first,
        page.locator("#quantity, input[name='quantity']")
    )
    await page.locator("#quantity, input[name='quantity']").fill("6000")
    await page.locator("input[name='commit'], input[value='送出']").click()
    await page.wait_for_load_state("domcontentloaded")

    # 7. 提现订单
    await search_account(final_account)
    await click_and_wait_element(
        page.locator("tbody tr").first.locator("a[href$='/withdraws']"),
        page.locator("a:has-text('輸入拼多多訂單'), a:has-text('輸入提現訂單'), a[href*='/withdraws/new']").first
    )
    withdraw_btn = page.locator("a:has-text('輸入拼多多訂單'), a:has-text('輸入提現訂單'), a[href*='/withdraws/new']").first
    await withdraw_btn.click()
    
    qty_input = page.locator("#quantity, input[name='quantity']")
    await qty_input.wait_for(state="visible", timeout=10000)
    await qty_input.fill("6000")
    await page.locator("input[name='commit'], input[value='送出']").click()
    await page.wait_for_load_state("domcontentloaded")
except Exception as sub_err:
    print(f"⚠️ 后续商品或订单初始化超时/失败（建店本身已成功）: {sub_err}")
