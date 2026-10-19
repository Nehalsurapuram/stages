package com.autoimmune.orders;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

/** F01 hidden edge cases. Labelling only: never mounted in the sandbox or shown to the pipeline. */
@SpringBootTest
@AutoConfigureMockMvc
class F01HiddenTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void emptyCustomerIsBadRequest() throws Exception {
        mvc.perform(get("/api/orders/search").param("customer", "")).andExpect(status().isBadRequest());
    }

    @Test
    void whitespaceCustomerIsBadRequest() throws Exception {
        mvc.perform(get("/api/orders/search").param("customer", "   ")).andExpect(status().isBadRequest());
    }

    @Test
    void paddedCustomerIsTrimmed() throws Exception {
        mvc.perform(get("/api/orders/search").param("customer", " alice "))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(2)));
    }
}
