import {expect,test} from '@playwright/test';

const digest='sha256:'+'a'.repeat(64);
const camera={
  camera_id:'cam-front',friendly_name:'Front entrance',configured:true,enabled:true,
  credentials_configured:true,manufacturer:'Example',model:'EdgeCam',updated_at:'2026-09-24T00:00:00Z',
  assignments:[{deployment_id:'dep-primary',apps:['anpr'],fps:8}],
};
const apexCamera={camera_id:'cam-front',name:'Front entrance',in_use:true,assigned_to:['dep-primary']};
const solution={
  catalog_id:'traffic-v4',solution_name:'Traffic',version:'4',hardware_profile:'intel-285h',
  status:'available',image:{digest,reference:'registry.example/traffic@'+digest},
  contract:{ui:{camera:{apps:{anpr:{},face_recognition:{}}}}},
};
const originalShape={
  shape_id:'11111111-1111-4111-8111-111111111111',kind:'line',
  shape_key:'front-gate_entry',name:'Front gate entry',
  points:[[0.1,0.5],[0.9,0.5]],role_key:'front-gate',direction:'entry',inside_side:'b',enabled:true,
};
const originalConfig={lines:[{
  id:'front-gate_entry',name:'Front gate entry',
  points:[[0.1,0.5],[0.9,0.5]],accepted:['A->B'],
}]};

function deploymentStatus(overrides={}){
  return {
    camera_id:'cam-front',geometry_revision:2,overall_state:'applied',
    deployments:[{
      deployment_id:'dep-primary',state:'applied',phase:'completed',
      desired_revision:5,applied_revision:5,
      desired_geometry_revision:2,applied_geometry_revision:2,
      last_error_code:null,retry_at:null,
    }],
    ...overrides,
  };
}

async function mockDashboard(page){
  let shape={...originalShape};
  let status=deploymentStatus();
  let previewBody=null;
  let updateBody=null;
  await page.route('**/dashboard/api/**',async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname;
    if(path==='/dashboard/api/customer')return route.fulfill({json:{site_id:'test-site',cameras:[apexCamera],deployments:[]}});
    if(path==='/dashboard/api/telemetry/events')return route.fulfill({json:{events:[],has_more:false}});
    if(path==='/dashboard/api/cameras/snapshot')return route.fulfill({status:404,json:{error:'unavailable'}});
    if(path==='/dashboard/api/v1/alerts')return route.fulfill({json:[]});
    if(path==='/dashboard/api/v1/cameras')return route.fulfill({json:[camera]});
    if(path==='/dashboard/api/v1/cameras/cam-front')return route.fulfill({json:camera});
    if(path==='/dashboard/api/v1/cameras/cam-front/snapshot')return route.fulfill({status:404,json:{detail:'unavailable'}});
    if(path==='/dashboard/api/v1/cameras/cam-front/geometry'){
      return route.fulfill({json:{camera_id:'cam-front',geometry_revision:status.geometry_revision,shapes:[shape],compiled_config:originalConfig}});
    }
    if(path==='/dashboard/api/v1/cameras/cam-front/deployment-status')return route.fulfill({json:status});
    if(path==='/dashboard/api/v1/cameras/cam-front/geometry/'+originalShape.shape_id&&request.method()==='PUT'){
      updateBody=request.postDataJSON();
      shape={...shape,...updateBody,shape_key:'front-gate_exit'};
      status=deploymentStatus({
        geometry_revision:3,overall_state:'pending',
        deployments:[{
          deployment_id:'dep-primary',state:'pending',phase:'validating',
          desired_revision:6,applied_revision:5,
          desired_geometry_revision:3,applied_geometry_revision:2,
          last_error_code:null,retry_at:null,
        }],
      });
      return route.fulfill({json:shape});
    }
    if(path==='/dashboard/api/v1/solutions')return route.fulfill({json:[solution]});
    if(path==='/dashboard/api/v1/deployments/preview'&&request.method()==='POST'){
      previewBody=request.postDataJSON();
      return route.fulfill({json:{bundle_sha256:'b'.repeat(64),image_reference:solution.image.reference}});
    }
    if(path==='/dashboard/api/v1/deployments/dep-primary/enrollment/camera'){
      return route.fulfill({json:{deployment_key:'dep-primary',camera_id:'cam-front'}});
    }
    if(path==='/dashboard/api/v1/deployments/dep-primary/enrollment/status'){
      return route.fulfill({json:{deployment_key:'dep-primary',designated_camera_id:'cam-front',session:null,degraded:false}});
    }
    return route.fulfill({status:404,json:{detail:'Unhandled test route: '+request.method()+' '+path}});
  });
  return {
    previewBody:()=>previewBody,
    updateBody:()=>updateBody,
  };
}

test('initial deployment always previews authoritative camera geometry',async({page})=>{
  const state=await mockDashboard(page);
  await page.goto('/dashboard');
  await page.getByRole('button',{name:'Cameras',exact:true}).click();
  await page.getByRole('button',{name:'Deploy solution'}).click();

  await page.locator('.deployment-camera-choice input').check();
  await expect(page.getByText(/1 saved shapes · revision 2/)).toBeVisible();
  await expect(page.locator('textarea')).toHaveCount(0);
  await page.getByRole('button',{name:'Preview deployment'}).click();

  await expect.poll(state.previewBody).not.toBeNull();
  expect(state.previewBody().assignments[0].config).toEqual(originalConfig);
});

test('saved lines are editable and expose pending deployment revisions',async({page})=>{
  const state=await mockDashboard(page);
  await page.goto('/dashboard');
  await page.getByRole('button',{name:/Front entrance/}).click();
  await page.getByRole('button',{name:'Manage'}).click();
  await page.getByRole('button',{name:'Zones & Lines'}).click();

  const row=page.locator('.cu-device-row').filter({hasText:'Front gate entry'});
  await row.getByRole('button',{name:'Edit'}).click();
  await expect(page.getByLabel('Line name')).toHaveValue('Front gate entry');
  await expect(page.getByLabel('Gate name')).toHaveValue('front-gate');
  await expect(page.getByLabel('This camera detects')).toHaveValue('entry');

  await page.getByLabel('Line name').fill('Front gate exit');
  await page.getByLabel('This camera detects').selectOption('exit');
  await page.getByRole('button',{name:'Update line'}).click();

  await expect.poll(state.updateBody).not.toBeNull();
  expect(state.updateBody()).toMatchObject({
    name:'Front gate exit',role_key:'front-gate',direction:'exit',inside_side:'b',
    points:[[0.1,0.5],[0.9,0.5]],
  });
  const panel=page.locator('.camera-deployment-status');
  await expect(panel.getByText('Pending',{exact:true}).first()).toBeVisible();
  await expect(panel.getByText(/5 \/ 6/)).toBeVisible();
  await expect(panel.getByText(/2 \/ 3/)).toBeVisible();
});
