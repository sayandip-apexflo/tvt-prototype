const {chromium}=require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const fs=require('fs'), path=require('path'), assert=require('assert');
const root=path.join(__dirname,'../apexfabric/control_plane/static');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 let rules=[], alerts=[], savedRule;
 const camera={camera_id:'front',name:'Front entrance',has_source:true,in_use:true,assigned_to:['traffic-runtime']};
 const node={metadata:{name:'edge-box',labels:{'node-role.kubernetes.io/control-plane':'','apexfabric.com/qualified':'true'}},status:{conditions:[{type:'Ready',status:'True'}]}};
 const deployment={metadata:{name:'traffic-runtime',labels:{'app.kubernetes.io/managed-by':'apexfabric-node-agent','apexfabric.com/deployment-id':'traffic'}},spec:{replicas:1,template:{spec:{containers:[{image:'127.0.0.1:5000/apexfabric/traffic:v1'}]}}},status:{readyReplicas:1}};
 await page.route('**/*',route=>{
  const p=new URL(route.request().url()).pathname;
  const files={'/apexfabricdashboard':'site.html','/apexfabricdashboard/site.js':'site.js','/apexfabricdashboard/site.css':'site.css','/dashboard':'customer.html','/dashboard/customer.js':'customer.js','/dashboard/alerts.js':'alerts.js','/dashboard/site.css':'site.css'};
  if(files[p])return route.fulfill({path:path.join(root,files[p]),contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':'text/html'});
  if(p.endsWith('/alert-rules')){
   if(route.request().method()==='POST'){
    const body=route.request().postDataJSON();
    savedRule={...body.rule,id:'a'.repeat(32),enabled:true};rules=[savedRule];
    alerts=[{id:'b'.repeat(32),created_at:Date.now()/1000,acknowledged_at:null,evidence:{rule:savedRule,camera_id:'front',deployment_id:'traffic-runtime',value:25}}];
    return route.fulfill({json:{rule:savedRule}});
   }
   return route.fulfill({json:{rules}});
  }
  if(p.endsWith('/alerts'))return route.fulfill({json:{alerts}});
  if(p.endsWith('/alerts/acknowledge')){alerts[0].acknowledged_at=Date.now()/1000;return route.fulfill({json:{ok:true}})}
  const data=p.endsWith('/status')?{site_id:'edge-test',nodes:[node],deployments:[deployment],pods:[],events:[],jobs:[]}:p.endsWith('/catalog')?{solutions:[]}:p.endsWith('/cameras')?{cameras:[camera]}:p.endsWith('/customer')?{site_id:'edge-test',cameras:[camera],deployments:[{name:'traffic-runtime',solution:'Traffic',status:'Running'}]}:p.endsWith('/telemetry/events')?{events:[{deployment_id:'traffic-runtime',payload:{id:'e1',type:'vehicle_count',camera_id:'front',data:{count:25}}}]}:null;
  if(data)return route.fulfill({json:data});
  return route.fulfill({status:404,json:{error:'not found'}});
 });
 await page.goto('http://edge.test/apexfabricdashboard');
 await page.getByText('edge-test',{exact:false}).first().waitFor();
 assert.equal(await page.locator('.box-card').count(),1);
 await page.locator('[data-view="devices"]').click();
 assert.equal(await page.locator('h1').textContent(),'Devices');
 await page.locator('[data-view="deployments"]').click();
 assert.equal(await page.locator('.solution-card').count(),1);
 await page.locator('[data-view="cameras"]').click();
 await page.locator('#show-camera-form').click();
 assert(await page.locator('#new-camera').isVisible());
 await page.locator('[data-view="overview"]').click();
 await page.screenshot({path:'/tmp/apexfabric-management.png',fullPage:true});
 await page.goto('http://edge.test/dashboard');
 await page.getByText('Front entrance',{exact:true}).first().waitFor();
 await page.getByText('Create an alert rule',{exact:true}).click();
 await page.locator('[name="name"]').fill('Busy entrance');
 await page.locator('[name="event_type"]').selectOption('vehicle_count');
 await page.locator('[name="field"]').selectOption('data.count');
 await page.locator('[name="threshold"]').fill('20');
 await page.getByRole('button',{name:'Create rule',exact:true}).click();
 await page.locator('dialog[open]').waitFor();
 assert.equal(savedRule.threshold,20);
 assert.equal(savedRule.field,'data.count');
 await page.locator('dialog [data-ack]').click();
 await page.locator('dialog[open]').waitFor({state:'hidden'});
 assert(alerts[0].acknowledged_at);
 await page.getByText('Create an alert rule',{exact:true}).click();
 assert(await page.getByText('Running',{exact:false}).isVisible());
 await page.screenshot({path:'/tmp/apexfabric-customer.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);
 console.log('UI smoke passed: management navigation, camera form, customer assignments, rule creation, alert popup/acknowledgement, mobile width, no JavaScript errors.');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
