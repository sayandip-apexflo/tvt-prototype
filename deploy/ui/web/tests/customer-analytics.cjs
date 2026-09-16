const {chromium}=require('playwright');
const assert=require('assert');

(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const now=new Date().toISOString(),camera={camera_id:'cam1',name:'Main Road',in_use:true,assigned_to:['traffic']};
  const event=(id,event_type,value,extra={})=>({event_id:id,deployment_id:'traffic',occurred_at:now,payload:{event_type,camera_id:'cam1',payload:{id,event_type,count:{total:value},...extra}},snapshots:[]});
  const events=[event('p','pedestrian_count_per_frame',13),event('v','vehicle_count_per_frame',8)];
  let siteId='test-site';
  await page.route('**/dashboard/api/customer',r=>r.fulfill({json:{site_id:siteId,cameras:[camera],deployments:[]}}));
  await page.route('**/dashboard/api/site',async r=>{siteId=(await r.request().postDataJSON()).site_id;await r.fulfill({json:{site_id:siteId}})});
  await page.route('**/dashboard/api/telemetry/events?*',r=>r.fulfill({json:{events}}));
  await page.route('**/dashboard/api/cameras/snapshot?*',r=>r.fulfill({status:404,json:{error:'test'}}));
  await page.goto('http://127.0.0.1:4174/dashboard');
  await page.getByText('Main Road',{exact:true}).first().click();
  await page.locator('.cu-recent-events h2').filter({hasText:'Camera activity'}).waitFor();
  assert.equal(await page.locator('.cu-activity-list>button').count(),2);
  assert((await page.locator('.cu-activity-list').textContent()).includes('People: 13 · Vehicles: 8'));
  await page.locator('.cu-theme-toggle').click();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByLabel('Site name').fill('Pune Demo Site');
  await page.getByRole('button',{name:'Save site name'}).click();
  await page.getByText('Site name saved.').waitFor();
  await page.getByRole('button',{name:'Alerts Config'}).click();
  assert(await page.getByText('The alerts layer is not enabled yet.',{exact:false}).isVisible());
  assert.deepEqual(errors,[]);
  console.log('Customer analytics UI passed: grouped count card, theme, site and alerts placeholder (no smoke/fire).');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
