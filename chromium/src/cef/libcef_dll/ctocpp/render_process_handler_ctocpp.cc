// Copyright (c) 2017 The Chromium Embedded Framework Authors. All rights
// reserved. Use of this source code is governed by a BSD-style license that
// can be found in the LICENSE file.
//
// ---------------------------------------------------------------------------
//
// This file was generated by the CEF translator tool. If making changes by
// hand only do so within the body of existing method and function
// implementations. See the translator.README.txt file in the tools directory
// for more information.
//

#include "libcef_dll/cpptoc/browser_cpptoc.h"
#include "libcef_dll/cpptoc/domnode_cpptoc.h"
#include "libcef_dll/cpptoc/frame_cpptoc.h"
#include "libcef_dll/cpptoc/list_value_cpptoc.h"
#include "libcef_dll/cpptoc/process_message_cpptoc.h"
#include "libcef_dll/cpptoc/request_cpptoc.h"
#include "libcef_dll/cpptoc/v8context_cpptoc.h"
#include "libcef_dll/cpptoc/v8exception_cpptoc.h"
#include "libcef_dll/cpptoc/v8stack_trace_cpptoc.h"
#include "libcef_dll/ctocpp/load_handler_ctocpp.h"
#include "libcef_dll/ctocpp/render_process_handler_ctocpp.h"


// VIRTUAL METHODS - Body may be edited by hand.

void CefRenderProcessHandlerCToCpp::OnRenderThreadCreated(
    CefRefPtr<CefListValue> extra_info) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_render_thread_created))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: extra_info; type: refptr_diff
  DCHECK(extra_info.get());
  if (!extra_info.get())
    return;

  // Execute
  _struct->on_render_thread_created(_struct,
      CefListValueCppToC::Wrap(extra_info));
}

void CefRenderProcessHandlerCToCpp::OnWebKitInitialized() {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_web_kit_initialized))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Execute
  _struct->on_web_kit_initialized(_struct);
}

void CefRenderProcessHandlerCToCpp::OnBrowserCreated(
    CefRefPtr<CefBrowser> browser) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_browser_created))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return;

  // Execute
  _struct->on_browser_created(_struct,
      CefBrowserCppToC::Wrap(browser));
}

void CefRenderProcessHandlerCToCpp::OnBrowserDestroyed(
    CefRefPtr<CefBrowser> browser) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_browser_destroyed))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return;

  // Execute
  _struct->on_browser_destroyed(_struct,
      CefBrowserCppToC::Wrap(browser));
}

CefRefPtr<CefLoadHandler> CefRenderProcessHandlerCToCpp::GetLoadHandler() {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, get_load_handler))
    return NULL;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Execute
  cef_load_handler_t* _retval = _struct->get_load_handler(_struct);

  // Return type: refptr_same
  return CefLoadHandlerCToCpp::Wrap(_retval);
}

bool CefRenderProcessHandlerCToCpp::OnBeforeNavigation(
    CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
    CefRefPtr<CefRequest> request, NavigationType navigation_type,
    bool is_redirect) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_before_navigation))
    return false;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return false;
  // Verify param: frame; type: refptr_diff
  DCHECK(frame.get());
  if (!frame.get())
    return false;
  // Verify param: request; type: refptr_diff
  DCHECK(request.get());
  if (!request.get())
    return false;

  // Execute
  int _retval = _struct->on_before_navigation(_struct,
      CefBrowserCppToC::Wrap(browser),
      CefFrameCppToC::Wrap(frame),
      CefRequestCppToC::Wrap(request),
      navigation_type,
      is_redirect);

  // Return type: bool
  return _retval?true:false;
}

void CefRenderProcessHandlerCToCpp::OnContextCreated(
    CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
    CefRefPtr<CefV8Context> context) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_context_created))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return;
  // Verify param: frame; type: refptr_diff
  DCHECK(frame.get());
  if (!frame.get())
    return;
  // Verify param: context; type: refptr_diff
  DCHECK(context.get());
  if (!context.get())
    return;

  // Execute
  _struct->on_context_created(_struct,
      CefBrowserCppToC::Wrap(browser),
      CefFrameCppToC::Wrap(frame),
      CefV8ContextCppToC::Wrap(context));
}

void CefRenderProcessHandlerCToCpp::OnContextReleased(
    CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
    CefRefPtr<CefV8Context> context) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_context_released))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return;
  // Verify param: frame; type: refptr_diff
  DCHECK(frame.get());
  if (!frame.get())
    return;
  // Verify param: context; type: refptr_diff
  DCHECK(context.get());
  if (!context.get())
    return;

  // Execute
  _struct->on_context_released(_struct,
      CefBrowserCppToC::Wrap(browser),
      CefFrameCppToC::Wrap(frame),
      CefV8ContextCppToC::Wrap(context));
}

void CefRenderProcessHandlerCToCpp::OnUncaughtException(
    CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
    CefRefPtr<CefV8Context> context, CefRefPtr<CefV8Exception> exception,
    CefRefPtr<CefV8StackTrace> stackTrace) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_uncaught_exception))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return;
  // Verify param: frame; type: refptr_diff
  DCHECK(frame.get());
  if (!frame.get())
    return;
  // Verify param: context; type: refptr_diff
  DCHECK(context.get());
  if (!context.get())
    return;
  // Verify param: exception; type: refptr_diff
  DCHECK(exception.get());
  if (!exception.get())
    return;
  // Verify param: stackTrace; type: refptr_diff
  DCHECK(stackTrace.get());
  if (!stackTrace.get())
    return;

  // Execute
  _struct->on_uncaught_exception(_struct,
      CefBrowserCppToC::Wrap(browser),
      CefFrameCppToC::Wrap(frame),
      CefV8ContextCppToC::Wrap(context),
      CefV8ExceptionCppToC::Wrap(exception),
      CefV8StackTraceCppToC::Wrap(stackTrace));
}

void CefRenderProcessHandlerCToCpp::OnFocusedNodeChanged(
    CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
    CefRefPtr<CefDOMNode> node) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_focused_node_changed))
    return;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return;
  // Unverified params: frame, node

  // Execute
  _struct->on_focused_node_changed(_struct,
      CefBrowserCppToC::Wrap(browser),
      CefFrameCppToC::Wrap(frame),
      CefDOMNodeCppToC::Wrap(node));
}

bool CefRenderProcessHandlerCToCpp::OnProcessMessageReceived(
    CefRefPtr<CefBrowser> browser, CefProcessId source_process,
    CefRefPtr<CefProcessMessage> message) {
  cef_render_process_handler_t* _struct = GetStruct();
  if (CEF_MEMBER_MISSING(_struct, on_process_message_received))
    return false;

  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  // Verify param: browser; type: refptr_diff
  DCHECK(browser.get());
  if (!browser.get())
    return false;
  // Verify param: message; type: refptr_diff
  DCHECK(message.get());
  if (!message.get())
    return false;

  // Execute
  int _retval = _struct->on_process_message_received(_struct,
      CefBrowserCppToC::Wrap(browser),
      source_process,
      CefProcessMessageCppToC::Wrap(message));

  // Return type: bool
  return _retval?true:false;
}


// CONSTRUCTOR - Do not edit by hand.

CefRenderProcessHandlerCToCpp::CefRenderProcessHandlerCToCpp() {
}

template<> cef_render_process_handler_t* CefCToCpp<CefRenderProcessHandlerCToCpp,
    CefRenderProcessHandler, cef_render_process_handler_t>::UnwrapDerived(
    CefWrapperType type, CefRenderProcessHandler* c) {
  NOTREACHED() << "Unexpected class type: " << type;
  return NULL;
}

#if DCHECK_IS_ON()
template<> base::AtomicRefCount CefCToCpp<CefRenderProcessHandlerCToCpp,
    CefRenderProcessHandler, cef_render_process_handler_t>::DebugObjCt = 0;
#endif

template<> CefWrapperType CefCToCpp<CefRenderProcessHandlerCToCpp,
    CefRenderProcessHandler, cef_render_process_handler_t>::kWrapperType =
    WT_RENDER_PROCESS_HANDLER;