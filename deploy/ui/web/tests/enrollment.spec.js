import {expect,test} from '@playwright/test';

const deployments=[{
  deployment_id:'dep-primary',catalog_id:'traffic-v4',namespace:'apexfabric',
  lifecycle_intent:'Running',sync_state:'applied',applied_revision:3,desired_revision:3,
  applied_image_digest:'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  desired_bundle_sha256:'bundle-current',bundle_history:[],
}];
const solutions=[{
  catalog_id:'traffic-v4',solution_name:'Traffic',version:'4',hardware_profile:'intel-285h',
  status:'available',image:{digest:'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',reference:'registry/traffic@sha256:aaaaaaaa'},
  contract:{ui:{camera:{apps:{face_recognition:{},anpr:{}}}}},
}];
const tvtCameras=[
  {camera_id:'cam-both',friendly_name:'Front entrance',configured:true,enabled:true,assignments:[
    {deployment_id:'dep-other',apps:['anpr'],fps:8},
    {deployment_id:'dep-primary',apps:['anpr','face_recognition'],fps:8},
  ]},
  {camera_id:'cam-face',friendly_name:'Reception',configured:true,enabled:true,assignments:[
    {deployment_id:'dep-primary',apps:['face_recognition'],fps:8},
  ]},
  {camera_id:'cam-anpr',friendly_name:'Vehicle gate',configured:true,enabled:true,assignments:[
    {deployment_id:'dep-primary',apps:['anpr'],fps:8},
  ]},
];
const apexCameras=tvtCameras.map(camera=>({
  camera_id:camera.camera_id,name:camera.friendly_name,in_use:true,
  assigned_to:camera.camera_id==='cam-both'?['raw-wrong-deployment']:['dep-primary'],
}));

async function mockDashboard(page,{active=false,postError=false,pendingName=false}={}){
  let designated='cam-both';
  let namingStatus=pendingName?'pending_name':null;
  const posts=[];
  const requests=[];
  await page.route('**/dashboard/api/**',async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname;
    requests.push(path);
    if(path==='/dashboard/api/customer')return route.fulfill({json:{site_id:'test-site',cameras:apexCameras,deployments:[]}});
    if(path==='/dashboard/api/telemetry/events')return route.fulfill({json:{events:[],has_more:false}});
    if(path==='/dashboard/api/cameras/snapshot')return route.fulfill({status:404,json:{error:'unavailable in test'}});
    if(path==='/dashboard/api/v1/alerts')return route.fulfill({json:[]});
    if(path==='/dashboard/api/v1/cameras')return route.fulfill({json:tvtCameras});
    if(path==='/dashboard/api/v1/solutions')return route.fulfill({json:solutions});
    if(path==='/dashboard/api/v1/deployments')return route.fulfill({json:deployments});
    const designation=path.match(/^\/dashboard\/api\/v1\/deployments\/([^/]+)\/enrollment\/camera$/);
    if(designation){
      const deploymentId=decodeURIComponent(designation[1]);
      if(request.method()==='POST'){
        const body=request.postDataJSON();posts.push(body);
        if(postError)return route.fulfill({status:409,json:{detail:'camera is no longer eligible for face enrollment'}});
        designated=body.camera_id;
      }
      const cameraId=deploymentId==='dep-primary'?designated:'another-camera';
      return route.fulfill({json:{deployment_key:deploymentId,camera_id:cameraId}});
    }
    const captured=()=>({session_id:'session-1',status:'completed',result_code:'ok',capture_result:'created',capture_count:3,naming_status:namingStatus});
    if(path==='/dashboard/api/v1/deployments/dep-primary/enrollment/sessions/session-1/cancel'){
      posts.push({cancel:true});
      namingStatus='discarded';
      return route.fulfill({status:409,json:{detail:'No record created for unnamed person'}});
    }
    if(path==='/dashboard/api/v1/deployments/dep-primary/enrollment/sessions/session-1/name'){
      posts.push(request.postDataJSON());
      namingStatus='named';
      return route.fulfill({json:captured()});
    }
    const status=path.match(/^\/dashboard\/api\/v1\/deployments\/([^/]+)\/enrollment\/status$/);
    if(status){
      const deploymentId=decodeURIComponent(status[1]);
      const session=namingStatus&&deploymentId==='dep-primary'?captured():active?{session_id:'session-1',status:'capturing'}:null;
      return route.fulfill({json:{deployment_key:deploymentId,designated_camera_id:deploymentId==='dep-primary'?designated:'another-camera',session,degraded:false}});
    }
    return route.fulfill({status:404,json:{detail:`Unhandled test route: ${request.method()} ${path}`}});
  });
  return {posts,requests};
}

test('site dashboard hides infrastructure views and deploys from Cameras',async({page})=>{
  const state=await mockDashboard(page);
  await page.goto('/dashboard');

  await expect(page.getByRole('button',{name:'Cameras',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Solutions',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Cluster',exact:true})).toHaveCount(0);
  await expect.poll(()=>state.requests.some(path=>path.startsWith('/dashboard/api/v1/cluster'))).toBe(false);

  await page.getByRole('button',{name:'Cameras',exact:true}).click();
  await page.getByRole('button',{name:'Deploy solution'}).click();
  await expect(page.locator('textarea')).toHaveCount(0);
  await expect(page.locator('.enrollment-designation')).toHaveCount(0);
  await expect(page.getByText(/Camera geometry is loaded from each camera/)).toBeVisible();
  await expect(page.getByLabel('CPU request')).toBeHidden();
  await expect(page.getByText(/registry\/traffic|intel-285h/)).toHaveCount(0);
  await expect.poll(()=>state.requests.includes('/dashboard/api/v1/deployments')).toBe(false);
});

test('Live enrollment follows every TVT assignment instead of raw assignment order',async({page})=>{
  await mockDashboard(page);
  await page.goto('/dashboard');

  await page.getByRole('button',{name:/Front entrance/}).click();
  await expect(page.getByRole('button',{name:'Start enrollment'})).toBeVisible();
  await expect(page.getByText('dep-primary',{exact:true})).toBeVisible();

  await page.getByRole('button',{name:'Cameras',exact:true}).click();
  await page.getByRole('button',{name:/Vehicle gate/}).click();
  await expect(page.getByRole('button',{name:'Start enrollment'})).toHaveCount(0);
});

test('Stopping enrollment before naming shows that no record was created',async({page})=>{
  const state=await mockDashboard(page,{pendingName:true});
  await page.goto('/dashboard');
  await page.getByRole('button',{name:/Front entrance/}).click();

  await expect(page.getByText(/Face captured \(3 frames\)/)).toBeVisible();
  await expect(page.getByRole('button',{name:'Start enrollment'})).toHaveCount(0);
  await page.getByRole('button',{name:'Stop enrollment'}).click();

  await expect(page.getByRole('alert').filter({hasText:'No record created for unnamed person'})).toBeVisible();
  await expect(page.getByLabel('Name this person')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Start enrollment'})).toBeVisible();
  expect(state.posts).toContainEqual({cancel:true});
});

test('Naming a captured face enrolls the person',async({page})=>{
  const state=await mockDashboard(page,{pendingName:true});
  await page.goto('/dashboard');
  await page.getByRole('button',{name:/Front entrance/}).click();

  await page.getByLabel('Name this person').fill('Jane Doe');
  await page.getByRole('button',{name:'Save name'}).click();

  await expect(page.getByText('Name saved.')).toBeVisible();
  await expect(page.getByRole('button',{name:'Stop enrollment'})).toHaveCount(0);
  expect(state.posts).toContainEqual({display_name:'Jane Doe'});
});
