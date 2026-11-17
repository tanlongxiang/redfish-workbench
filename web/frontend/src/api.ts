let csrf = ''
export function setCsrf(value:string){csrf=value}
export async function api<T=any>(path:string,method='GET',body?:unknown):Promise<T>{
  const headers:Record<string,string>={}
  if(method!=='GET'&&method!=='HEAD')headers['X-CSRF-Token']=csrf
  const form=body instanceof FormData
  if(body!==undefined&&!form)headers['Content-Type']='application/json'
  const response=await fetch(path,{method,headers,credentials:'same-origin',body:body===undefined?undefined:form?body:JSON.stringify(body)})
  if(!response.ok){
    const error=await response.json().catch(()=>({detail:'服务响应异常'}))
    throw new Error(error.detail||`请求失败 (${response.status})`)
  }
  return response.json()
}
