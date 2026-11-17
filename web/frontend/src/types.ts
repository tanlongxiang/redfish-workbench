export type Mode='demo'|'live'
export const COMPONENTS=['Dcu02Cpld','Dcu13Cpld','Dcu46Cpld','Dcu57Cpld'] as const
export interface Snapshot{model:string;power:string;inventory:Record<string,{version:string}>;[key:string]:unknown}
export interface NodeRow{name:string;address:string;snapshot?:Snapshot;observed?:number;status?:string;progress?:number}
export interface ImageMeta{id:string;name:string;sha256:string;size:number;demo:boolean;created?:number}
export interface Group{component:string;enabled:boolean;imageId:string;expected:string}
export interface Draft{operation?:string;title?:string;image_name?:string;image_sha256?:string;revision:string;mode:Mode;targets:{name:string;address:string}[];images:{component:string;image_id:string;expected:string}[];model?:string;auth:string;self_signed:boolean;workers:number;canary_confirmed:boolean}
export interface Job{id:string;action:string;mode:Mode;state:string;created:number;started?:number;finished?:number;error?:string;draft?:Draft;results?:any[];has_report?:boolean;stop_requested?:number}
export interface EventRow{id:number;at:number;node?:string;address?:string;event:string;data:Record<string,any>}
