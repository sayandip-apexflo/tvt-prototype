import {expect,test} from '@playwright/test';

async function mockDashboard(page){
  const rawPersonId='0f4b5276483f4faeb8c915c0675feee3';
  const events=[
    {session_id:'session-1',person_id:'named-person',display_name:'Asha Rao',camera_id:'cam-main',gate:'Main gate',action:'entry',time:1790223210},
    {session_id:'session-2',person_id:rawPersonId,display_name:null,camera_id:'cam-side',gate:'Side gate',action:'entry',time:1790222954},
  ];
  await page.route('**/dashboard/api/**',async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname;
    if(path==='/dashboard/api/customer')return route.fulfill({json:{site_id:'test-site',cameras:[{camera_id:'cam-main',name:'Main entrance',in_use:true},{camera_id:'cam-side',name:'Side entrance',in_use:true}],deployments:[]}});
    if(path==='/dashboard/api/telemetry/events')return route.fulfill({json:{events:[],has_more:false}});
    if(path==='/dashboard/api/v1/alerts')return route.fulfill({json:[]});
    if(path==='/dashboard/api/v1/cameras')return route.fulfill({json:[]});
    if(path==='/dashboard/api/v1/reports/attendance-log')return route.fulfill({json:{events}});
    if(path==='/dashboard/api/v1/reports/attendance')return route.fulfill({json:{sessions:[],total_duration_seconds:0}});
    return route.fulfill({status:404,json:{detail:'Unhandled test route: '+request.method()+' '+path}});
  });
  return {rawPersonId};
}

test('Recent activity shows employee names without naming controls or person IDs',async({page})=>{
  const state=await mockDashboard(page);
  await page.goto('/dashboard');
  await page.getByRole('button',{name:'Reports',exact:true}).click();

  await expect(page.getByText('Asha Rao entered',{exact:false})).toBeVisible();
  await expect(page.getByText('Unnamed employee entered',{exact:false})).toBeVisible();
  await expect(page.getByText(state.rawPersonId,{exact:false})).toHaveCount(0);
  await expect(page.locator('.cu-attendance-list > li').first()).toHaveCSS('padding','8px');
  const camera=page.getByText('Main entrance (cam-main)',{exact:true});
  await expect(camera).toHaveCSS('padding','4px');
  await expect(page.getByRole('button',{name:'Name employee'})).toHaveCount(0);
});

test('Camera analytics shows face and plate events only for the selected camera',async({page})=>{
  const events=[
    {event_id:'face-main',occurred_at:'2026-09-24T08:30:00Z',received_at:1790238600,payload:{event_type:'face_detection_event',camera_id:'cam-main',payload:{subject:{type:'face',bbox:[0.1,0.1,0.2,0.2]}}},snapshots:[]},
    {event_id:'face-other',occurred_at:'2026-09-24T08:29:00Z',received_at:1790238540,payload:{event_type:'face_detection_event',camera_id:'cam-other',payload:{subject:{type:'face',bbox:[0.1,0.1,0.2,0.2]}}},snapshots:[]},
    {event_id:'plate-main',occurred_at:'2026-09-24T08:28:00Z',received_at:1790238480,payload:{event_type:'plate_read_event',camera_id:'cam-main',payload:{plate:{text:'TEST123'}}},snapshots:[]},
    {event_id:'motion-main',occurred_at:'2026-09-24T08:27:00Z',received_at:1790238420,payload:{event_type:'motion_event',camera_id:'cam-main'},snapshots:[]},
  ];
  await page.route('**/dashboard/api/**',async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname;
    if(path==='/dashboard/api/customer')return route.fulfill({json:{site_id:'test-site',cameras:[{camera_id:'cam-main',name:'Main entrance',in_use:true},{camera_id:'cam-other',name:'Other entrance',in_use:true}],deployments:[]}});
    if(path==='/dashboard/api/telemetry/events')return route.fulfill({json:{events}});
    if(path==='/dashboard/api/v1/alerts')return route.fulfill({json:[]});
    if(path==='/dashboard/api/v1/cameras')return route.fulfill({json:[]});
    return route.fulfill({status:404,json:{detail:'Unhandled test route: '+request.method()+' '+path}});
  });

  await page.goto('/dashboard');
  await page.getByRole('button',{name:'Cameras',exact:true}).click();
  await page.locator('.cu-camera-card').filter({hasText:'Main entrance'}).click();

  await expect(page.getByRole('heading',{name:'Camera analytics events'})).toBeVisible();
  await expect(page.getByText('Face detected',{exact:true})).toHaveCount(1);
  await expect(page.getByText('Plate TEST123 read',{exact:true})).toBeVisible();
  await expect(page.locator('.cu-recent-events .cu-event-list > button')).toHaveCount(2);
});
