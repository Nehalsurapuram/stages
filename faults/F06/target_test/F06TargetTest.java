package com.autoimmune.orders;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

/** F06: an order totalling exactly 1000.00 must get the 10% discount. */
@SpringBootTest
@AutoConfigureMockMvc
class F06TargetTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void orderAtExactlyThresholdIsDiscounted() throws Exception {
        mvc.perform(get("/api/orders/2/discount"))
                .andExpect(status().isOk())
                .andExpect(content().string("900.00"));
    }
}
