package com.autoimmune.orders;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

/** F01: search without a customer must be rejected with 400, not fail with a NullPointerException. */
@SpringBootTest
@AutoConfigureMockMvc
class F01TargetTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void searchWithoutCustomerIsBadRequest() throws Exception {
        mvc.perform(get("/api/orders/search")).andExpect(status().isBadRequest());
    }
}
