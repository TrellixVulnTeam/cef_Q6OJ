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

#include "libcef_dll/cpptoc/auth_callback_cpptoc.h"


namespace {

// MEMBER FUNCTIONS - Body may be edited by hand.

void CEF_CALLBACK auth_callback_cont(struct _cef_auth_callback_t* self,
    const cef_string_t* username, const cef_string_t* password) {
  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  DCHECK(self);
  if (!self)
    return;
  // Verify param: username; type: string_byref_const
  DCHECK(username);
  if (!username)
    return;
  // Verify param: password; type: string_byref_const
  DCHECK(password);
  if (!password)
    return;

  // Execute
  CefAuthCallbackCppToC::Get(self)->Continue(
      CefString(username),
      CefString(password));
}

void CEF_CALLBACK auth_callback_cancel(struct _cef_auth_callback_t* self) {
  // AUTO-GENERATED CONTENT - DELETE THIS COMMENT BEFORE MODIFYING

  DCHECK(self);
  if (!self)
    return;

  // Execute
  CefAuthCallbackCppToC::Get(self)->Cancel();
}

}  // namespace


// CONSTRUCTOR - Do not edit by hand.

CefAuthCallbackCppToC::CefAuthCallbackCppToC() {
  GetStruct()->cont = auth_callback_cont;
  GetStruct()->cancel = auth_callback_cancel;
}

template<> CefRefPtr<CefAuthCallback> CefCppToC<CefAuthCallbackCppToC,
    CefAuthCallback, cef_auth_callback_t>::UnwrapDerived(CefWrapperType type,
    cef_auth_callback_t* s) {
  NOTREACHED() << "Unexpected class type: " << type;
  return NULL;
}

#if DCHECK_IS_ON()
template<> base::AtomicRefCount CefCppToC<CefAuthCallbackCppToC,
    CefAuthCallback, cef_auth_callback_t>::DebugObjCt = 0;
#endif

template<> CefWrapperType CefCppToC<CefAuthCallbackCppToC, CefAuthCallback,
    cef_auth_callback_t>::kWrapperType = WT_AUTH_CALLBACK;
