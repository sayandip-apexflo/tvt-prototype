import {expect,test} from '@playwright/test';

const node={
  metadata:{name:'edge-box-1',uid:'node-uid-1',labels:{
    'kubernetes.io/arch':'amd64','apexfabric.com/hardware-profile':'intel-285h',
    'apexfabric.com/qualified':'true','node-role.kubernetes.io/control-plane':'',
  }},
  status:{
    conditions:[{type:'Ready',status:'True'}],
    nodeInfo:{osImage:'Ubuntu 24.04',kubeletVersion:'v1.31.0+k3s1'},
    capacity:{'apexfabric.com/camera-streams':'8'},allocatable:{'apexfabric.com/camera-streams':'6'},
  },
};
const deployment={
  metadata:{name:'traffic-runtime',labels:{'apexfabric.com/application':'runtime'}},
  spec:{replicas:1,template:{spec:{containers:[{image:'registry.example/traffic@sha256:abc'}]}}},
  status:{readyReplicas:1,availableReplicas:1},
};
const status={
  site_id:'test-site',nodes:[node],deployments:[deployment],
  pods:[{metadata:{name:'traffic-runtime-abc',creationTimestamp:'2026-09-24T00:00:00Z'},spec:{nodeName:'edge-box-1'},status:{phase:'Running',containerStatuses:[{ready:true,restartCount:2}]}}],
  services:[{metadata:{name:'traffic-runtime'},spec:{type:'ClusterIP',clusterIP:'10.43.0.10',ports:[{port:8080,protocol:'TCP'}]}}],
  replica_sets:[],persistent_volume_claims:[{metadata:{name:'traffic-state'},spec:{storageClassName:'local-path'},status:{phase:'Bound',capacity:{storage:'50Gi'}}}],
  events:[{metadata:{uid:'e1'},type:'Warning',reason:'BackOff',message:'Back-off pulling image',count:3,involvedObject:{kind:'Pod',name:'traffic-runtime-abc'},lastTimestamp:'2026-09-24T00:01:00Z'}],
  node_reports:[{metadata:{name:'edge-box-1'},spec:{observedAt:'2026-09-24T00:00:00Z',capabilities:{npu:true}},status:{reason:'hardware profile matched'}}],
};
const solution={
  catalog_id:'traffic-v4',name:'traffic-edge-runtime',version:'2026.08.21-v4',status:'available',
  contract:{hardwareProfile:'intel-285h',ui:{displayName:'Traffic analytics'}},
  image:{repository:'127.0.0.1:5000/traffic',tag:'v4',digest:'sha256:'+'a'.repeat(64)},
};

async function mockAdmin(page){
  const requests=[];
  await page.route('**/apexfabricdashboard/api/**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    requests.push(path);
    if(path==='/apexfabricdashboard/api/status')return route.fulfill({json:status});
    if(path==='/apexfabricdashboard/api/catalog')return route.fulfill({json:{solutions:[solution]}});
    if(path==='/apexfabricdashboard/api/catalog/refresh')return route.fulfill({json:{solutions:[solution]}});
    if(path==='/apexfabricdashboard/api/cameras')return route.fulfill({json:{cameras:[]}});
    if(path==='/apexfabricdashboard/api/device-registry')return route.fulfill({json:{devices:[]}});
    if(path==='/apexfabricdashboard/api/telemetry/storage')return route.fulfill({json:{events:0,snapshots:0,logical_bytes:0}});
    if(path==='/apexfabricdashboard/api/workload-telemetry')return route.fulfill({json:{deployment:'traffic-runtime',available:true,health:{ok:true},readiness:{ready:true},kubernetes:{ready_replicas:1},metrics:'up 1'}});
    return route.fulfill({status:404,json:{error:'Unhandled test route: '+path}});
  });
  return requests;
}

test('Admin Cluster shows all namespace objects from apexfabric-control only',async({page})=>{
  const requests=await mockAdmin(page);
  await page.goto('/apexfabricdashboard/');
  await expect(page.getByRole('button',{name:'Boxes'})).toHaveCount(0);
  await page.getByRole('button',{name:'Cluster'}).click();

  await expect(page.getByText('edge-box-1')).toBeVisible();
  await expect(page.getByText('Ubuntu 24.04')).toBeVisible();
  await expect(page.getByText('6 / 8')).toBeVisible();
  await page.getByRole('button',{name:'Details'}).click();
  await expect(page.locator('.modal').getByText(/Reason: hardware profile matched/)).toBeVisible();
  await expect(page.locator('.modal').getByText(/"npu": true/)).toBeVisible();
  await page.getByRole('button',{name:'Close'}).click();

  await page.getByRole('button',{name:'Deployments',exact:true}).last().click();
  await page.getByRole('button',{name:'Telemetry'}).click();
  await expect(page.getByText('up 1')).toBeVisible();
  await page.getByRole('button',{name:'Close'}).click();

  await page.getByRole('button',{name:'Events'}).click();
  await expect(page.getByText('Back-off pulling image')).toBeVisible();
  await page.getByRole('button',{name:'Storage'}).click();
  await expect(page.getByText('50Gi')).toBeVisible();

  expect(requests.some(path=>path.includes('/api/v1/'))).toBe(false);
});

test('Admin Catalog lists solutions and refreshes through apexfabric-control',async({page})=>{
  const requests=await mockAdmin(page);
  await page.goto('/apexfabricdashboard/');
  await page.getByRole('button',{name:'Catalog'}).click();
  await expect(page.getByText('Traffic analytics')).toBeVisible();
  await expect(page.getByText('127.0.0.1:5000/traffic:v4')).toBeVisible();
  await page.getByRole('button',{name:'Refresh catalog'}).click();
  await expect.poll(()=>requests.includes('/apexfabricdashboard/api/catalog/refresh')).toBe(true);
});
